"""Encode Apple Health workouts as Garmin-compatible FIT activity files."""

from __future__ import annotations

import math
from bisect import bisect_left
from datetime import timezone
from pathlib import Path

from garmin_fit_sdk import Encoder, Profile

from .apple_export import Workout

FIT_EPOCH_S = 631065600  # 1984-12-31 00:00:00 UTC

MESG_FILE_ID = Profile["mesg_num"]["FILE_ID"]
MESG_EVENT = Profile["mesg_num"]["EVENT"]
MESG_RECORD = Profile["mesg_num"]["RECORD"]
MESG_LAP = Profile["mesg_num"]["LAP"]
MESG_SESSION = Profile["mesg_num"]["SESSION"]
MESG_ACTIVITY = Profile["mesg_num"]["ACTIVITY"]
MESG_DEVICE_INFO = Profile["mesg_num"]["DEVICE_INFO"]

_SPORT = {v: k for k, v in Profile["types"]["sport"].items()}
_SUB_SPORT = {v: k for k, v in Profile["types"]["sub_sport"].items()}
_FILE = {v: k for k, v in Profile["types"]["file"].items()}
_EVENT = {v: k for k, v in Profile["types"]["event"].items()}
_EVENT_TYPE = {v: k for k, v in Profile["types"]["event_type"].items()}
_ACTIVITY_TYPE = {v: k for k, v in Profile["types"]["activity_type"].items()}

_SPORT_GENERIC = _SPORT["generic"]
_SUB_SPORT_GENERIC = _SUB_SPORT["generic"]

# HK workoutActivityType -> (sport, sub_sport); unknown names degrade to generic.
HK_SPORT_MAP = {
    "HKWorkoutActivityTypeRunning": ("running", "generic"),
    "HKWorkoutActivityTypeWalking": ("walking", "generic"),
    "HKWorkoutActivityTypeCycling": ("cycling", "generic"),
    "HKWorkoutActivityTypeSwimming": ("swimming", "generic"),
    "HKWorkoutActivityTypeHiking": ("hiking", "generic"),
    "HKWorkoutActivityTypeYoga": ("fitness_equipment", "yoga"),
    "HKWorkoutActivityTypeFunctionalStrengthTraining": (
        "fitness_equipment",
        "strength_training",
    ),
    "HKWorkoutActivityTypeTraditionalStrengthTraining": (
        "fitness_equipment",
        "strength_training",
    ),
    "HKWorkoutActivityTypeCoreTraining": ("fitness_equipment", "cardio_training"),
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": ("hiit", "generic"),
    "HKWorkoutActivityTypeRowing": ("rowing", "generic"),
    "HKWorkoutActivityTypeElliptical": ("fitness_equipment", "indoor_cardio"),
    "HKWorkoutActivityTypeClimbing": ("floor_climbing", "generic"),
    "HKWorkoutActivityTypeSkatingSports": ("inline_skating", "generic"),
    "HKWorkoutActivityTypeDancing": ("dance", "generic"),
    "HKWorkoutActivityTypeTaiChi": ("flexibility_training", "generic"),
}

_MANUFACTURER = 0  # developer/invalid — data provenance is Apple Health


def _fit_ts(dt) -> int:
    return int(dt.astimezone(timezone.utc).timestamp()) - FIT_EPOCH_S


def _semi(deg: float) -> int:
    return int(round(deg * (2**31 / 180.0)))


def _haversine_m(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _sport_for(activity_type: str) -> tuple[int, int]:
    mapped = HK_SPORT_MAP.get(activity_type)
    if mapped is None:
        return _SPORT_GENERIC, _SUB_SPORT_GENERIC
    sport, sub = mapped
    return _SPORT.get(sport, _SPORT_GENERIC), _SUB_SPORT.get(sub, _SUB_SPORT_GENERIC)


def _nearest_hr(hr_list, ts, tolerance_s: float = 5.0):
    """Return bpm of the HR sample closest to ts within tolerance, else None."""
    if not hr_list:
        return None
    keys = [s.ts for s in hr_list]
    i = bisect_left(keys, ts)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(hr_list):
            delta = abs((hr_list[j].ts - ts).total_seconds())
            if delta <= tolerance_s and (best is None or delta < best[0]):
                best = (delta, hr_list[j].bpm)
    return best[1] if best else None


PAUSE_EVENTS = (
    "pause",
    "resume",
    "HKWorkoutEventTypePause",
    "HKWorkoutEventTypeResume",
    "HKWorkoutEventTypeMotionPaused",
    "HKWorkoutEventTypeMotionResumed",
)


def _laps(workout: Workout) -> list[tuple]:
    """Split the workout into (start_ts, end_ts) lap segments on pause/resume."""
    bounds = [workout.start]
    for e in workout.events:
        if e.type in PAUSE_EVENTS and workout.start < e.start < workout.end:
            bounds.append(e.start)
    bounds.append(workout.end)
    bounds.sort()
    paused = any(
        e.type in ("pause", "HKWorkoutEventTypePause", "HKWorkoutEventTypeMotionPaused")
        for e in workout.events
    )
    PAUSE = ("pause", "HKWorkoutEventTypePause", "HKWorkoutEventTypeMotionPaused")
    RESUME = ("resume", "HKWorkoutEventTypeResume", "HKWorkoutEventTypeMotionResumed")
    segments = []
    if paused:
        # Walk events: active from start until 'pause', from 'resume' until next
        # pause/end.
        cur = workout.start
        for e in sorted(workout.events, key=lambda e: e.start):
            if e.type in PAUSE and e.start > cur:
                segments.append((cur, e.start))
                cur = None
            elif e.type in RESUME:
                cur = e.start
        if cur is not None:
            segments.append((cur, workout.end))
        if not segments:
            segments = [(workout.start, workout.end)]
    else:
        segments = [(workout.start, workout.end)]
    return segments


def _records(workout: Workout):
    """Yield FIT record field dicts for one workout.

    With GPS: records at each GPS point, HR attached by nearest sample.
    Without GPS: records on the HR timeline, distance linearly interpolated.
    """
    recs = []
    if workout.gps:
        dist_m = 0.0
        prev = None
        for p in workout.gps:
            if prev is not None:
                dist_m += _haversine_m(prev.lat, prev.lon, p.lat, p.lon)
            prev = p
            r = {
                "timestamp": _fit_ts(p.ts),
                "position_lat": _semi(p.lat),
                "position_long": _semi(p.lon),
                "distance": round(dist_m, 1),
            }
            if p.ele is not None:
                r["altitude"] = round(p.ele, 1)
            hr = _nearest_hr(workout.hr, p.ts)
            if hr is not None:
                r["heart_rate"] = int(round(hr))
            recs.append(r)
        # Scale GPS-derived distance to Apple's official total when it exists
        # (removes cumulative GPS drift).
        total_m = workout.total_distance * 1000.0
        if total_m > 0 and dist_m > 0 and len(recs) > 1:
            scale = total_m / dist_m
            acc = 0.0
            prev_d = 0.0
            for r in recs:
                acc = r["distance"] * scale
                r["distance"] = round(acc, 1)
    elif workout.hr:
        n = len(workout.hr)
        total_m = workout.total_distance * 1000.0
        for i, s in enumerate(workout.hr):
            r = {"timestamp": _fit_ts(s.ts), "heart_rate": int(round(s.bpm))}
            if total_m > 0 and n > 1:
                r["distance"] = round(total_m * i / (n - 1), 1)
            recs.append(r)
    # Enforce strictly non-decreasing, unique timestamps.
    out = []
    last = None
    for r in recs:
        if last is None or r["timestamp"] > last:
            out.append(r)
            last = r["timestamp"]
    return out


def build_fit(workout: Workout) -> bytes:
    """Encode one workout as a FIT activity file's bytes."""
    recs = _records(workout)
    if not recs:
        raise ValueError(f"no record data for {workout.id}")
    laps = _laps(workout)
    sport, sub_sport = _sport_for(workout.activity_type)
    start_ts = _fit_ts(workout.start)
    end_ts = _fit_ts(workout.end)
    total_m = workout.total_distance * 1000.0
    duration = max(workout.duration, (workout.end - workout.start).total_seconds())

    hr_vals = [s.bpm for s in workout.hr] or []
    avg_hr = workout.hr_avg or (sum(hr_vals) / len(hr_vals) if hr_vals else None)
    max_hr = workout.hr_max or (max(hr_vals) if hr_vals else None)

    enc = Encoder()

    enc.write_mesg(
        {
            "mesg_num": MESG_FILE_ID,
            "time_created": end_ts,
            "type": _FILE.get("activity"),
            "manufacturer": _MANUFACTURER,
        }
    )
    enc.write_mesg(
        {
            "mesg_num": MESG_DEVICE_INFO,
            "timestamp": start_ts,
            "manufacturer": _MANUFACTURER,
            "product": 0,
        }
    )
    enc.write_mesg(
        {
            "mesg_num": MESG_EVENT,
            "timestamp": start_ts,
            "event": _EVENT.get("timer"),
            "event_type": _EVENT_TYPE.get("start"),
        }
    )
    for r in recs:
        r = {"mesg_num": MESG_RECORD, **r}
        enc.write_mesg(r)

    # Laps
    lap_distance_acc = 0.0
    for idx, (lo, hi) in enumerate(laps):
        in_lap = [r for r in recs if _ts_between(r, lo, hi)]
        if not in_lap:
            continue
        elapsed = (hi - lo).total_seconds()
        enc.write_mesg(
            {
                "mesg_num": MESG_LAP,
                "message_index": idx,
                "timestamp": in_lap[-1]["timestamp"],
                "start_time": _fit_ts(lo),
                "total_elapsed_time": round(elapsed, 3),
                "total_timer_time": round(elapsed, 3),
                "total_distance": round(
                    (in_lap[-1].get("distance") or 0) - lap_distance_acc, 1
                ),
                "event": _EVENT.get("timer"),
                "event_type": _EVENT_TYPE.get("stop",
                                              _EVENT_TYPE.get("stop_all")),
            }
        )
        lap_distance_acc = in_lap[-1].get("distance") or lap_distance_acc

    session = {
        "mesg_num": MESG_SESSION,
        "message_index": 0,
        "sport": sport,
        "sub_sport": sub_sport,
        "start_time": start_ts,
        "timestamp": recs[-1]["timestamp"],
        "total_elapsed_time": round(
            (workout.end - workout.start).total_seconds(), 3
        ),
        "total_timer_time": round(duration, 3),
        "first_lap_index": 0,
        "num_laps": len(laps),
        "event": _EVENT.get("timer"),
        "event_type": _EVENT_TYPE.get("stop_all"),
    }
    if total_m > 0:
        session["total_distance"] = round(total_m, 1)
    if workout.total_energy > 0:
        session["total_calories"] = int(round(workout.total_energy))
    if avg_hr:
        session["avg_heart_rate"] = int(round(avg_hr))
    if max_hr:
        session["max_heart_rate"] = int(round(max_hr))
    enc.write_mesg(session)

    enc.write_mesg(
        {
            "mesg_num": MESG_ACTIVITY,
            "timestamp": recs[-1]["timestamp"],
            "num_sessions": 1,
            "type": _ACTIVITY_TYPE.get("auto", _ACTIVITY_TYPE.get("generic")),
            "event": _EVENT.get("activity"),
            "event_type": _EVENT_TYPE.get("stop"),
        }
    )
    return enc.close()


def _ts_between(record: dict, lo, hi) -> bool:
    lo_s, hi_s = _fit_ts(lo), _fit_ts(hi)
    return lo_s <= record["timestamp"] <= hi_s


def fit_filename(workout: Workout) -> str:
    # End time disambiguates same-second twins (Keep writes two workouts with
    # the same type and start second but different durations).
    ts = workout.start.strftime("%Y-%m-%d_%H%M%S")
    end = workout.end.strftime("%H%M%S")
    kind = workout.activity_type.replace("HKWorkoutActivityType", "")
    return f"{ts}_{end}_{kind}.fit"


def write_fit(workout: Workout, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / fit_filename(workout)
    data = build_fit(workout)
    path.write_bytes(data)
    return path
