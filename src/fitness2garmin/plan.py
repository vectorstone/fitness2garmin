"""Build the migration plan: compare Garmin inventory with Apple Health workouts.

Matching rule: Garmin activity startTimeLocal vs Apple workout local start time
within ±120 s on the same day. A matched pair is 'replace' (delete Garmin +
upload complete Apple version) when duration or distance disagree beyond
tolerance; otherwise 'skip'. Unmatched Apple workouts are 'upload'. Garmin-only
activities are reported but never touched.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .apple_export import Workout, parse_dt

MATCH_TOLERANCE = timedelta(seconds=120)
DURATION_TOL = 0.2  # 20%
DURATION_ABS_TOL = 30.0  # seconds
DISTANCE_TOL = 0.3  # 30%


@dataclass
class GarminActivity:
    id: int | str
    name: str
    start_local: datetime  # naive local
    duration_s: float | None
    distance_m: float | None
    type_key: str
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "GarminActivity":
        summary = d.get("summaryDTO") or {}
        start = d.get("startTimeLocal") or d.get("startTimeGMT")
        try:
            start_dt = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            try:
                start_dt = datetime.strptime(str(start)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                start_dt = datetime.min
        return cls(
            id=d.get("activityId"),
            name=d.get("activityName", ""),
            start_local=start_dt,
            duration_s=summary.get("duration"),
            distance_m=summary.get("distance"),
            type_key=(d.get("activityTypeDTO") or {}).get("typeKey", ""),
            raw=d,
        )


def _parse_local(s: str) -> datetime:
    dt = parse_dt(s)
    return dt.replace(tzinfo=None) if dt else datetime.min


@dataclass
class PlanItem:
    action: str  # upload | replace | skip | garmin_only
    workout: Workout | None = None
    garmin: GarminActivity | None = None
    reason: str = ""


def build_plan(
    workouts: list[Workout], garmin_items: list[dict]
) -> list[PlanItem]:
    gacts = sorted(
        (GarminActivity.from_api(d) for d in garmin_items),
        key=lambda a: a.start_local,
    )
    gstarts = [a.start_local for a in gacts]
    plan: list[PlanItem] = []
    matched_garmin: set[int] = set()

    for w in sorted(workouts, key=lambda w: w.start):
        w_local = w.start.replace(tzinfo=None)
        i = bisect_left(gstarts, w_local - MATCH_TOLERANCE)
        match = None
        while i < len(gacts) and gacts[i].start_local <= w_local + MATCH_TOLERANCE:
            if gacts[i].id not in matched_garmin:
                match = gacts[i]
                break
            i += 1
        if match is None:
            plan.append(PlanItem(action="upload", workout=w, reason="not in Garmin"))
            continue
        matched_garmin.add(match.id)
        reason = _mismatch_reason(w, match)
        if reason:
            plan.append(
                PlanItem(action="replace", workout=w, garmin=match, reason=reason)
            )
        else:
            plan.append(PlanItem(action="skip", workout=w, garmin=match))
    for a in gacts:
        if a.id not in matched_garmin:
            plan.append(PlanItem(action="garmin_only", garmin=a))
    return plan


def _mismatch_reason(w: Workout, g: GarminActivity) -> str:
    reasons = []
    w_duration = max(w.duration, (w.end - w.start).total_seconds())
    if g.duration_s is not None:
        if (
            abs(g.duration_s - w_duration) > DURATION_ABS_TOL
            and abs(g.duration_s - w_duration) > DURATION_TOL * max(g.duration_s, w_duration)
        ):
            reasons.append(
                f"duration garmin={g.duration_s:.0f}s apple={w_duration:.0f}s"
            )
    w_dist_m = w.total_distance * 1000.0
    if w_dist_m > 0 and not g.distance_m:
        reasons.append("distance missing on garmin")
    elif g.distance_m and w_dist_m <= 0:
        pass  # Apple workout without distance is not "more complete"
    elif w_dist_m > 0 and g.distance_m:
        if abs(g.distance_m - w_dist_m) > DISTANCE_TOL * max(g.distance_m, w_dist_m):
            reasons.append(
                f"distance garmin={g.distance_m:.0f}m apple={w_dist_m:.0f}m"
            )
    if w.gps and not g.distance_m:
        reasons.append("apple has GPS track, garmin has none")
    return "; ".join(reasons)


def summarize(plan: list[PlanItem]) -> dict[str, int]:
    counts = {"upload": 0, "replace": 0, "skip": 0, "garmin_only": 0}
    for p in plan:
        counts[p.action] += 1
    return counts
