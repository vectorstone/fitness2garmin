"""Match Keep export records against the Garmin inventory → keep_plan.json.

Verdicts:
  upload     — not in Garmin, real Keep-only workout
  replace    — matched a garbage-duration Garmin activity (Keep double-write
               shells migrated via Apple Health); Keep holds the real data
  skip       — already in Garmin (Apple-era imports, native watch activities,
               or same-session "Other" twins recorded by Apple Watch)
  junk       — no Garmin match, under 60 s, no HR data
  twin_ghost — Keep double-write shell of the following real record
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MATCH_TOLERANCE = timedelta(seconds=120)
GHOST_SPAN_S = 4 * 3600  # a 4h+ "run"/"walk" is a double-write shell
JUNK_MAX_S = 60.0

# Keep type -> Garmin activityType.typeKey it may correspond to.
TYPE_COMPAT = {
    "跑步": {"running", "treadmill_running"},
    "行走": {"walking"},
    "骑行": {"cycling", "indoor_cycling", "virtual_ride", "gravel_cycling_unstructured",
             "mountain_biking", "trail_running", "street_biking"},
    "健身": None,  # Apple recorded these as various types — matched by overlap
}


@dataclass
class KeepPlanItem:
    action: str  # upload | replace | skip | junk | twin_ghost
    n: int  # Keep record number (1-based xlsx row)
    garmin_id: int | None = None
    reason: str = ""

    def to_json(self) -> dict:
        return {"action": self.action, "n": self.n,
                "garmin_id": self.garmin_id, "reason": self.reason}


def _ghost_twins(records):
    """Keep double-write shells: same type, same start second, absurd span."""
    ordered = sorted(records, key=lambda r: (r.start_utc, r.n))
    ghosts: dict[int, int] = {}  # ghost n -> real n
    for i, w1 in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            w2 = ordered[j]
            gap = (w2.start_utc - w1.start_utc).total_seconds()
            if gap > 60:
                break
            if w1.type != w2.type:
                continue
            span1 = (w1.end_local - w1.start_local).total_seconds()
            span2 = (w2.end_local - w2.start_local).total_seconds()
            if span1 > span2 * 3 and span1 > GHOST_SPAN_S:
                long_, short_ = w1, w2
            elif span2 > span1 * 3 and span2 > GHOST_SPAN_S:
                long_, short_ = w2, w1
            else:
                continue
            if long_.n not in ghosts:
                ghosts[long_.n] = short_.n
    return ghosts


def _overlaps(r, g_start: datetime, g_dur: float) -> bool:
    """True when the Garmin activity and the Keep record are one session
    (overlap >= 80% of the shorter). Used for 健身, whose Garmin twin may be
    typed 'other'/'swimming'/etc. by Apple Watch."""
    k_start, k_end = r.start_utc, r.end_utc_as_utc()
    g_end = g_start + timedelta(seconds=g_dur or 0)
    ov = (min(k_end, g_end) - max(k_start, g_start)).total_seconds()
    shorter = min((k_end - k_start).total_seconds(), g_dur or 0, 1)
    return ov / shorter >= 0.8


def build_keep_plan(records, inventory: list[dict]) -> list[KeepPlanItem]:
    acts = []
    for a in inventory:
        gmt = a.get("startTimeGMT")
        if not gmt:
            continue
        try:
            start = datetime.strptime(str(gmt)[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        acts.append({
            "id": a["activityId"],
            "start": start,
            "type": (a.get("activityType") or {}).get("typeKey", ""),
            "dur": a.get("duration") or 0.0,
            "gps": bool(a.get("hasPolyline")),
            "name": a.get("activityName", ""),
        })
    acts.sort(key=lambda g: g["start"])
    starts = [g["start"] for g in acts]
    matched: set[int] = set()
    ghosts = _ghost_twins(records)

    items: list[KeepPlanItem] = []
    for r in sorted(records, key=lambda r: r.start_utc):
        if r.n in ghosts:
            items.append(KeepPlanItem("twin_ghost", r.n,
                                      reason=f"shell of #{ghosts[r.n]}"))
            continue
        i = bisect_left(starts, r.start_utc - MATCH_TOLERANCE)
        match = None
        while i < len(starts) and starts[i] <= r.start_utc + MATCH_TOLERANCE:
            g = acts[i]
            if g["id"] not in matched:
                compat = TYPE_COMPAT.get(r.type)
                if compat is None:
                    if _overlaps(r, g["start"], g["dur"]):
                        match = g
                        break
                elif g["type"] in compat:
                    match = g
                    break
            i += 1
        if match is None:
            if r.duration_s < JUNK_MAX_S and not r.hr:
                items.append(KeepPlanItem("junk", r.n, reason="<60s, no data"))
            else:
                items.append(KeepPlanItem("upload", r.n, reason="not in Garmin"))
            continue
        matched.add(match["id"])
        compat = TYPE_COMPAT.get(r.type)
        dur_gap = abs(match["dur"] - r.duration_s)
        dur_bad = (dur_gap > 30 and dur_gap > 0.2 * max(match["dur"], r.duration_s))
        if compat is not None and dur_bad and not match["gps"]:
            # type-compatible twin with an absurd duration: the Keep double-write
            # shell went to Apple Health and from there to Garmin — the Keep
            # record holds the real session.
            items.append(KeepPlanItem(
                "replace", r.n, garmin_id=match["id"],
                reason=(f"garbage duration {match['dur']:.0f}s vs keep "
                        f"{r.duration_s:.0f}s ({match['name']})")))
        else:
            why = ""
            if match["gps"]:
                why = "garmin copy has GPS track"
            elif compat is None:
                why = "same session, recorded by Apple Watch"
            items.append(KeepPlanItem("skip", r.n, garmin_id=match["id"], reason=why))
    return items


def summarize(items: list[KeepPlanItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for it in items:
        counts[it.action] = counts.get(it.action, 0) + 1
    return counts
