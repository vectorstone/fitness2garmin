"""Stream-parse Apple Health export.zip.

Extracts, in two streaming passes over export.xml (no full-document load):
  - every <Workout> with events, route references and HR statistics
  - heart-rate records that fall inside a workout window
  - body mass records (for Garmin weigh-in upload)
plus GPS tracks from workout-routes/*.gpx members of the archive.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile

HR_TYPE = "HKQuantityTypeIdentifierHeartRate"
BODY_MASS_TYPE = "HKQuantityTypeIdentifierBodyMass"
BODY_FAT_TYPE = "HKQuantityTypeIdentifierBodyFatPercentage"

# Activity types that normally carry a GPS route.
GPS_TYPES = {
    "HKWorkoutActivityTypeRunning",
    "HKWorkoutActivityTypeWalking",
    "HKWorkoutActivityTypeCycling",
    "HKWorkoutActivityTypeHiking",
    "HKWorkoutActivityTypeSwimming",
    "HKWorkoutActivityTypeDownhillSkiing",
    "HKWorkoutActivityTypeCrossCountrySkiing",
    "HKWorkoutActivityTypeSnowboarding",
}

_DT_FORMATS = ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S.%f %z")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


@dataclass
class HRSample:
    ts: datetime
    bpm: float


@dataclass
class GPXPoint:
    ts: datetime
    lat: float
    lon: float
    ele: float | None


@dataclass
class WorkoutEvent:
    type: str
    start: datetime


@dataclass
class Workout:
    activity_type: str  # e.g. HKWorkoutActivityTypeRunning
    start: datetime
    end: datetime
    duration: float  # seconds
    total_distance: float  # km
    total_energy: float  # kcal
    source: str
    events: list[WorkoutEvent] = field(default_factory=list)
    route_ref: str | None = None
    hr: list[HRSample] = field(default_factory=list)
    gps: list[GPXPoint] = field(default_factory=list)
    hr_avg: float | None = None
    hr_max: float | None = None

    @property
    def id(self) -> str:
        # Stable identity: activity type + local start time.
        return f"{self.activity_type}@{self.start.isoformat()}"


@dataclass
class BodyMass:
    ts: datetime
    kg: float
    fat_percent: float | None = None


# --- XML streaming -----------------------------------------------------------


def _find_export_member(zf: zipfile.ZipFile) -> str:
    candidates = [
        n for n in zf.namelist() if n.lower().endswith("export.xml")
    ]
    if candidates:
        return candidates[0]
    # Localized exports (e.g. 导出.xml): any xml whose head declares HealthData.
    for n in zf.namelist():
        if n.lower().endswith(".xml"):
            with zf.open(n) as fh:
                head = fh.read(256)
            if b"HealthData" in head:
                return n
    raise FileNotFoundError("no export.xml member found in archive")


def _iterparse(fh):
    """Yield (event, elem) while keeping memory bounded."""
    depth = 0
    root = None
    for event, elem in ET.iterparse(fh, events=("start", "end")):
        if event == "start":
            if root is None:
                root = elem
            depth += 1
            yield event, elem, depth
        else:
            depth -= 1
            yield event, elem, depth
            # Drop accumulated child stubs under the root periodically.
            if depth == 1 and elem.tag != "Workout":
                elem.clear()
                if getattr(root, "tag", None) and depth == 1:
                    try:
                        root.clear()
                    except Exception:
                        pass


def _parse_workout_elem(elem) -> Workout | None:
    start = parse_dt(elem.get("startDate"))
    end = parse_dt(elem.get("endDate"))
    if start is None or end is None:
        return None
    events: list[WorkoutEvent] = []
    route_ref = None
    hr_avg = hr_max = None
    for child in elem:
        tag = child.tag
        if tag == "WorkoutEvent":
            ev_start = parse_dt(child.get("startDate"))
            if ev_start is not None:
                events.append(WorkoutEvent(child.get("type", ""), ev_start))
        elif tag == "FileReference":
            route_ref = child.get("path")
        elif tag == "WorkoutStatistics":
            if child.get("type") == HR_TYPE:
                hr_avg = child.get("average")
                hr_max = child.get("maximum")
                hr_avg = float(hr_avg) if hr_avg else None
                hr_max = float(hr_max) if hr_max else None
    # Route reference may also appear via metadata entries in some exports.
    if route_ref is None:
        for child in elem.iter():
            if child.tag == "FileReference":
                route_ref = child.get("path")
                break
    return Workout(
        activity_type=elem.get("workoutActivityType", "HKWorkoutActivityTypeOther"),
        start=start,
        end=end,
        duration=float(elem.get("duration") or (end - start).total_seconds()),
        total_distance=float(elem.get("totalDistance") or 0) or 0,
        total_energy=float(elem.get("totalEnergyBurned") or 0) or 0,
        source=elem.get("sourceName", ""),
        events=events,
        route_ref=route_ref,
        hr_avg=hr_avg,
        hr_max=hr_max,
    )


def _parse_pass1(zf: zipfile.ZipFile, member: str) -> list[Workout]:
    workouts: list[Workout] = []
    with zf.open(member) as fh:
        in_workout = False
        for event, elem, depth in _iterparse(fh):
            tag = elem.tag
            if event == "start":
                if tag == "Workout" and depth == 2:
                    in_workout = True
                continue
            # end events
            if in_workout:
                if tag == "Workout":
                    w = _parse_workout_elem(elem)
                    if w is not None:
                        workouts.append(w)
                    in_workout = False
                    elem.clear()
                # children of Workout are read at the parent's end; keep them
            else:
                elem.clear()
    return workouts


def _attach_hr_and_body(zf: zipfile.ZipFile, member: str, workouts: list[Workout]):
    """Second pass: collect HR samples inside workout windows + body mass."""
    windows = sorted(workouts, key=lambda w: w.start)
    starts = [w.start for w in windows]
    body: dict[str, BodyMass] = {}

    with zf.open(member) as fh:
        for event, elem, depth in _iterparse(fh):
            if event == "start" or elem.tag != "Record":
                if event == "end":
                    elem.clear()
                continue
            rtype = elem.get("type", "")
            ts = parse_dt(elem.get("startDate"))
            value = elem.get("value")
            if ts is None or value is None:
                elem.clear()
                continue
            if rtype == HR_TYPE:
                i = bisect_right(starts, ts) - 1
                for j in (i, i - 1):
                    if 0 <= j < len(windows):
                        w = windows[j]
                        if w.start <= ts <= w.end:
                            w.hr.append(HRSample(ts, float(value)))
                            break
            elif rtype == BODY_MASS_TYPE or rtype == BODY_FAT_TYPE:
                unit = elem.get("unit", "kg")
                try:
                    v = float(value)
                except ValueError:
                    elem.clear()
                    continue
                if rtype == BODY_MASS_TYPE:
                    kg = v * 0.45359237 if unit in ("lb", "lbs", "pounds") else v
                    key = ts.strftime("%Y-%m-%d")
                    bm = body.get(key) or BodyMass(ts, kg)
                    if ts < bm.ts:  # keep earliest sample of the day
                        body[key] = BodyMass(ts, kg, fat_percent=bm.fat_percent)
                    else:
                        bm.kg = kg
                        body[key] = bm
                else:  # body fat percentage
                    key = ts.strftime("%Y-%m-%d")
                    bm = body.get(key) or BodyMass(ts, 0.0)
                    bm.fat_percent = v
                    body[key] = bm
            elem.clear()
    for w in workouts:
        w.hr.sort(key=lambda s: s.ts)
    return sorted(body.values(), key=lambda b: b.ts)


# --- GPX routes ---------------------------------------------------------------


def _parse_gpx(data: bytes) -> list[GPXPoint]:
    root = ET.fromstring(data)
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    points = []
    for trkpt in root.iter("{http://www.topografix.com/GPX/1/1}trkpt"):
        lat = trkpt.get("lat")
        lon = trkpt.get("lon")
        ele_text = trkpt.findtext("g:ele", namespaces=ns)
        time_text = trkpt.findtext("g:time", namespaces=ns)
        if not (lat and lon and time_text):
            continue
        try:
            ts = datetime.fromisoformat(time_text.replace("Z", "+00:00"))
        except ValueError:
            continue
        points.append(
            GPXPoint(
                ts=ts,
                lat=float(lat),
                lon=float(lon),
                ele=float(ele_text) if ele_text else None,
            )
        )
    points.sort(key=lambda p: p.ts)
    return points


def _load_routes(zf: zipfile.ZipFile, workouts: list[Workout]):
    members = [n for n in zf.namelist() if n.lower().endswith(".gpx")]
    by_base = {Path(n).name: n for n in members}
    used: set[str] = set()
    want_route = []
    for w in workouts:
        ref = w.route_ref
        member = by_base.get(Path(ref).name) if ref else None
        if member:
            w.gps = _parse_gpx(zf.read(member))
            used.add(member)
        elif w.activity_type in GPS_TYPES:
            want_route.append(w)
    if not want_route:
        return
    # Fallback: bucket unclaimed route files by their first trackpoint time.
    for member in members:
        if member in used:
            continue
        pts = _parse_gpx(zf.read(member))
        if not pts:
            continue
        t0 = pts[0].ts
        for w in want_route:
            if w.gps:
                continue
            lo = min(w.start, w.end)
            hi = max(w.start, w.end)
            if lo <= t0 <= hi:
                w.gps = pts
                used.add(member)
                break


# --- top-level API ------------------------------------------------------------


def parse_export(zip_path: str | Path) -> tuple[list[Workout], list[BodyMass]]:
    with zipfile.ZipFile(zip_path) as zf:
        member = _find_export_member(zf)
        workouts = _parse_pass1(zf, member)
        body = _attach_hr_and_body(zf, member, workouts)
        _load_routes(zf, workouts)
    workouts.sort(key=lambda w: w.start)
    return workouts, body


# --- JSON serialization --------------------------------------------------------


def workouts_to_json(workouts: list[Workout]) -> list[dict]:
    out = []
    for w in workouts:
        out.append(
            {
                "id": w.id,
                "activity_type": w.activity_type,
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "duration": w.duration,
                "total_distance_km": w.total_distance,
                "total_energy_kcal": w.total_energy,
                "source": w.source,
                "events": [
                    {"type": e.type, "start": e.start.isoformat()} for e in w.events
                ],
                "hr": [[s.ts.isoformat(), s.bpm] for s in w.hr],
                "gps": [
                    [p.ts.isoformat(), p.lat, p.lon, p.ele] for p in w.gps
                ],
                "hr_avg": w.hr_avg,
                "hr_max": w.hr_max,
            }
        )
    return out


def workouts_from_json(items: list[dict]) -> list[Workout]:
    out = []
    for d in items:
        w = Workout(
            activity_type=d["activity_type"],
            start=parse_dt(d["start"]),
            end=parse_dt(d["end"]),
            duration=d["duration"],
            total_distance=d["total_distance_km"],
            total_energy=d["total_energy_kcal"],
            source=d.get("source", ""),
            events=[
                WorkoutEvent(e["type"], parse_dt(e["start"]))
                for e in d.get("events", [])
            ],
            hr=[
                HRSample(parse_dt(ts), bpm) for ts, bpm in d.get("hr", [])
            ],
            gps=[
                GPXPoint(parse_dt(ts), lat, lon, ele)
                for ts, lat, lon, ele in d.get("gps", [])
            ],
            hr_avg=d.get("hr_avg"),
            hr_max=d.get("hr_max"),
        )
        out.append(w)
    return out


def bodymass_to_json(items: list[BodyMass]) -> list[dict]:
    return [
        {"ts": b.ts.isoformat(), "kg": b.kg, "fat_percent": b.fat_percent}
        for b in items
    ]


def bodymass_from_json(items: list[dict]) -> list[BodyMass]:
    return [
        BodyMass(parse_dt(d["ts"]), d["kg"], d.get("fat_percent"))
        for d in items
    ]
