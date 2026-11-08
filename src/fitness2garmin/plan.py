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
    has_gps: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "GarminActivity":
        # The activities list endpoint puts distance/duration at the top level
        # (activityType.typeKey); the detail endpoint nests them in
        # summaryDTO/activityTypeDTO. Accept both shapes.
        start = d.get("startTimeLocal") or d.get("startTimeGMT")
        try:
            start_dt = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            try:
                start_dt = datetime.strptime(str(start)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                start_dt = datetime.min
        atype = d.get("activityType") or d.get("activityTypeDTO") or {}
        return cls(
            id=d.get("activityId"),
            name=d.get("activityName", ""),
            start_local=start_dt,
            duration_s=d.get("duration")
            or (d.get("summaryDTO") or {}).get("duration"),
            distance_m=d.get("distance")
            or (d.get("summaryDTO") or {}).get("distance"),
            type_key=atype.get("typeKey", ""),
            has_gps=bool(d.get("hasPolyline")),
            raw=d,
        )


def _parse_local(s: str) -> datetime:
    dt = parse_dt(s)
    return dt.replace(tzinfo=None) if dt else datetime.min


@dataclass
class PlanItem:
    action: str  # upload | replace | skip | garmin_only | apple_dup | no_data
    workout: Workout | None = None
    garmin: GarminActivity | None = None
    reason: str = ""


def _data_score(w: Workout) -> int:
    score = len(w.hr) + 2 * len(w.gps)
    if w.hr_avg is not None or w.hr_max is not None:
        score += 8  # WorkoutStatistics present — the detailed twin
    if w.route_ref:
        score += 4
    if w.total_distance > 0:
        score += 2
    if w.total_energy > 0:
        score += 1
    return score


TWIN_OVERLAP_FRACTION = 0.8


def _dedupe_apple(workouts: list[Workout]) -> tuple[list[Workout], dict[str, str]]:
    """Collapse near-identical overlapping same-type workouts to one.

    Apps like Keep write both a summary shell and a detailed workout for the
    same session; wearing Apple Watch while recording with a phone app does
    the same. If two same-type workouts overlap by >= 80% of the shorter one,
    they are twins of a single physical session: keep the richer, drop the
    other. Returns (kept workouts, {dropped id: kept id}).
    """
    dropped: dict[str, str] = {}
    items = sorted(workouts, key=lambda w: w.start)
    keep = [True] * len(items)
    for i in range(len(items)):
        if not keep[i]:
            continue
        w1 = items[i]
        for j in range(i + 1, min(i + 4, len(items))):
            w2 = items[j]
            if w2.start >= w1.end:
                break
            if w1.activity_type != w2.activity_type:
                continue
            overlap = (min(w1.end, w2.end) - w2.start).total_seconds()
            shorter = min(
                (w1.end - w1.start).total_seconds(),
                (w2.end - w2.start).total_seconds(),
                w1.duration or 0,
                w2.duration or 0,
            ) or 1
            if overlap / shorter < TWIN_OVERLAP_FRACTION:
                continue
            rich, poor = (w1, w2) if _data_score(w1) >= _data_score(w2) else (w2, w1)
            poor_idx = j if poor is w2 else i
            if not keep[poor_idx]:
                continue
            keep[poor_idx] = False
            dropped[poor.id] = rich.id
    kept = [w for w, k in zip(items, keep) if k]
    return kept, dropped


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

    kept, dropped = _dedupe_apple(workouts)
    for w in workouts:
        if w.id in dropped:
            plan.append(
                PlanItem(
                    action="apple_dup",
                    workout=w,
                    reason=f"empty twin of {dropped[w.id]}",
                )
            )

    for w in sorted(kept, key=lambda w: w.start):
        if not w.hr and not w.gps:
            plan.append(PlanItem(action="no_data", workout=w, reason="no HR/GPS in export"))
            continue
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
    if w.gps and not g.has_gps:
        reasons.append("apple has GPS track, garmin has none")
    return "; ".join(reasons)


def summarize(plan: list[PlanItem]) -> dict[str, int]:
    counts = {
        "upload": 0,
        "replace": 0,
        "skip": 0,
        "garmin_only": 0,
        "apple_dup": 0,
        "no_data": 0,
    }
    for p in plan:
        counts[p.action] = counts.get(p.action, 0) + 1
    return counts
