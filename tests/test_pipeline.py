"""Self-contained integration test: synthetic export.zip → parse → FIT → decode → plan.

Run: .venv/bin/python tests/test_pipeline.py
No network, no Garmin account needed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fitness2garmin.apple_export import (  # noqa: E402
    parse_dt,
    parse_export,
    workouts_from_json,
    workouts_to_json,
)
from fitness2garmin.fit_builder import build_fit, fit_filename  # noqa: E402
from fitness2garmin.plan import build_plan, summarize  # noqa: E402

WORKOUT_START = datetime.strptime(
    "2026-01-10 07:00:00 +0800", "%Y-%m-%d %H:%M:%S %z"
)
WORKOUT_END = WORKOUT_START + timedelta(minutes=30)


def _xml() -> str:
    hr_records = []
    # HR every 30s inside the run window (07:00–07:30), plus one outside.
    t = WORKOUT_START
    bpm = 130
    while t <= WORKOUT_END:
        hr_records.append(
            f'<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" '
            f'unit="count/min" startDate="{t.strftime("%Y-%m-%d %H:%M:%S %z")}" '
            f'endDate="{t.strftime("%Y-%m-%d %H:%M:%S %z")}" value="{bpm}"/>'
        )
        bpm = min(bpm + 1, 170)
        t += timedelta(seconds=30)
    hr_records.append(
        '<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" '
        'unit="count/min" startDate="2026-02-01 03:00:00 +0800" '
        'endDate="2026-02-01 03:00:00 +0800" value="60"/>'
    )
    body = (
        '<Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg" '
        'startDate="2026-01-05 08:00:00 +0800" endDate="2026-01-05 08:00:00 +0800" value="72.5"/>'
    )
    # 2026 export format: duration in minutes, distance/energy in WorkoutStatistics
    run = (
        '<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" '
        'durationUnit="min" '
        f'startDate="{WORKOUT_START.strftime("%Y-%m-%d %H:%M:%S %z")}" '
        f'endDate="{WORKOUT_END.strftime("%Y-%m-%d %H:%M:%S %z")}" '
        'sourceName="Apple Watch">'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="150" maximum="175" sum="9000"/>'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" sum="5.0" unit="km"/>'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" sum="350" unit="Cal"/>'
        '<WorkoutEvent type="HKWorkoutEventTypeMotionPaused" startDate="2026-01-10 07:10:00 +0800"/>'
        '<WorkoutEvent type="HKWorkoutEventTypeMotionResumed" startDate="2026-01-10 07:11:00 +0800"/>'
        '<WorkoutRoute><FileReference path="/com.apple.health.workout-routes/route1.gpx"/></WorkoutRoute>'
        "</Workout>"
    )
    # legacy export format (duration in seconds, totalDistance attr) for compat
    gym = (
        '<Workout workoutActivityType="HKWorkoutActivityTypeTraditionalStrengthTraining" '
        'duration="2400" startDate="2026-01-12 19:00:00 +0800" '
        'endDate="2026-01-12 19:40:00 +0800" totalDistance="0" totalEnergyBurned="200" '
        'sourceName="Apple Watch">'
        "</Workout>"
    )
    # Keep-style empty twin of the run (same window, no data) — must be
    # deduped to apple_dup by the plan.
    shell = (
        '<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
        'duration="1800" startDate="2026-01-10 07:01:00 +0800" '
        'endDate="2026-01-10 07:31:00 +0800" totalDistance="0" totalEnergyBurned="0" '
        'sourceName="Keep">'
        "</Workout>"
    )
    # Gym workout has no HR in-window in this fixture on purpose? No — add some.
    gym_hr = [
        '<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" '
        'unit="count/min" startDate="2026-01-12 19:05:00 +0800" '
        'endDate="2026-01-12 19:05:00 +0800" value="110"/>',
        '<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" '
        'unit="count/min" startDate="2026-01-12 19:20:00 +0800" '
        'endDate="2026-01-12 19:20:00 +0800" value="125"/>',
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<HealthData exportDate="2026-11-08 10:00:00 +0800" locale="zh_CN">'
        + body + "".join(hr_records) + "".join(gym_hr) + run + gym + shell
        + "</HealthData>"
    )


def _gpx() -> str:
    # 181 points, every 10 s, matching the run window (UTC = -8 h).
    utc0 = WORKOUT_START.astimezone(tz=None).utcoffset()
    t_utc = WORKOUT_START - utc0
    pts = []
    lat, lon = 39.9042, 116.4074
    for i in range(181):
        ts = (t_utc + timedelta(seconds=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pts.append(
            f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>40.0</ele>'
            f"<time>{ts}</time></trkpt>"
        )
        lat += 0.00005
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="test" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
        "<trk><name>route1</name><trkseg>" + "".join(pts) + "</trkseg></trk></gpx>"
    )


def make_fixture(dirpath: Path) -> Path:
    zip_path = dirpath / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("apple_health_export/export.xml", _xml())
        zf.writestr("apple_health_export/workout-routes/route1.gpx", _gpx())
    return zip_path


def test_parse(workouts, body):
    assert len(workouts) == 3, workouts  # run, gym, keep-shell
    run = workouts[0]
    assert run.activity_type == "HKWorkoutActivityTypeRunning"
    assert run.total_distance == 5.0  # from WorkoutStatistics, not an attribute
    assert run.total_energy == 350.0
    assert run.duration == 1800.0  # duration="30" durationUnit="min"
    # 61 in-run HR samples; the outside one (2026-02-01) must be dropped.
    assert len(run.hr) == 61, len(run.hr)
    assert all(WORKOUT_START <= s.ts <= WORKOUT_END for s in run.hr)
    assert len(run.gps) == 181, len(run.gps)
    assert run.hr_avg == 150.0 and run.hr_max == 175.0
    assert [e.type for e in run.events] == [
        "HKWorkoutEventTypeMotionPaused",
        "HKWorkoutEventTypeMotionResumed",
    ]
    gym = workouts[2]
    assert len(gym.hr) == 2
    assert gym.gps == []
    shell = workouts[1]
    # attach-all: the twin sees in-window HR too (starts 07:01, so 59 not 61);
    # the plan stage later dedupes it in favour of the richer run
    assert shell.source == "Keep" and not shell.gps and len(shell.hr) == 59
    assert len(body) == 1 and body[0].kg == 72.5


def test_json_roundtrip(workouts):
    data = json.loads(json.dumps(workouts_to_json(workouts)))
    back = workouts_from_json(data)
    assert len(back) == 3
    assert back[0].id == workouts[0].id
    assert len(back[0].hr) == len(workouts[0].hr)
    assert len(back[0].gps) == len(workouts[0].gps)
    assert back[2].activity_type == workouts[2].activity_type


def test_fit(workouts):
    from garmin_fit_sdk import Decoder, Stream

    run, gym = workouts[0], workouts[2]
    for w, expect_records in ((run, 181), (gym, 2)):
        data = build_fit(w)
        path = Path(tempfile.mkdtemp()) / fit_filename(w)
        path.write_bytes(data)
        messages = []
        dec = Decoder(Stream.from_file(str(path)))
        dec.read(mesg_listener=lambda mesg_num, mesg: messages.append((mesg_num, mesg)))
        by_kind = {}
        for num, mesg in messages:
            by_kind.setdefault(num, []).append(mesg)
        assert 20 in by_kind, f"records missing in {w.id}"  # 20 = record
        records = by_kind[20]
        assert len(records) == expect_records, (len(records), expect_records)
        assert 18 in by_kind, "session missing"  # 18 = session
        assert 19 in by_kind, "lap missing"  # 19 = lap
        assert 34 in by_kind, "activity missing"  # 34 = activity
        # Run: records carry GPS + HR; distance monotonic.
        if expect_records > 100:
            lats = [r.get("position_lat") for r in records]
            assert all(l is not None for l in lats)
            hrs = [r.get("heart_rate") for r in records]
            assert any(h is not None for h in hrs), "run FIT has no HR"
            dists = [r["distance"] for r in records if r.get("distance") is not None]
            assert dists == sorted(dists), "distance not monotonic"
            assert abs(dists[-1] - 5000) < 50, f"distance {dists[-1]} != 5000 m"
            session = by_kind[18][0]
            assert session.get("sport") == "running", session.get("sport")
            assert session.get("total_distance", 0) > 0
            assert session.get("total_calories") == 350
            # pause at 07:10, resume 07:11 -> 2 active segments -> 2 laps
            assert len(by_kind[19]) == 2, len(by_kind[19])
        else:
            session = by_kind[18][0]
            assert session.get("sport") == "fitness_equipment", session.get("sport")
            assert session.get("sub_sport") is not None
        print(f"  [fit ok] {w.id}: {len(records)} records, "
              f"sport={session.get('sport')}, laps={len(by_kind.get(19, []))}")


def test_plan(workouts):
    # Garmin inventory: one complete match (skip), one short-duration match
    # (replace), one activity with no Apple counterpart (garmin_only).
    # 111 matches the run completely -> skip (new top-level shape, hasPolyline)
    # 222 matches the gym but duration mismatches -> replace (legacy summaryDTO shape)
    # 333 has no Apple counterpart -> garmin_only
    garmin = [
        {
            "activityId": 111,
            "activityName": "Run",
            "startTimeLocal": "2026-01-10 07:00:30",
            "duration": 1790,
            "distance": 5000,
            "hasPolyline": True,
            "activityType": {"typeKey": "running"},
        },
        {
            "activityId": 222,
            "activityName": "Gym",
            "startTimeLocal": "2026-01-12 19:00:10",
            "summaryDTO": {"duration": 100, "distance": None},
            "activityTypeDTO": {"typeKey": "strength_training"},
        },
        {
            "activityId": 333,
            "activityName": "Old ride",
            "startTimeLocal": "2020-05-01 09:00:00",
            "duration": 3600,
            "distance": 40000,
            "hasPolyline": True,
            "activityType": {"typeKey": "cycling"},
        },
    ]
    plan = build_plan(workouts, garmin)
    counts = summarize(plan)
    assert counts["skip"] == 1, counts
    assert counts["replace"] == 1, counts
    assert counts["garmin_only"] == 1, counts
    assert counts["upload"] == 0, counts
    # Keep-style empty twin of the run is deduped, not uploaded
    assert counts["apple_dup"] == 1, counts
    rep = [p for p in plan if p.action == "replace"][0]
    assert "duration" in rep.reason


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        zip_path = make_fixture(td)
        workouts, body = parse_export(zip_path)
        test_parse(workouts, body)
        print("[ok] parse")
        test_json_roundtrip(workouts)
        print("[ok] json roundtrip")
        test_fit(workouts)
        print("[ok] fit encode/decode")
        test_plan(workouts)
        print("[ok] plan")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
