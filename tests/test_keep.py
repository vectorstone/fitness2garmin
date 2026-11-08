"""Self-contained test for the Keep export pipeline: parse → plan → FIT.

Run: .venv/bin/python tests/test_keep.py
No network, no Garmin account needed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openpyxl import Workbook
from garmin_fit_sdk import Decoder, Encoder, Profile, Stream

from fitness2garmin.keep_export import (  # noqa: E402
    KeepRecord,
    parse_keep_export,
    records_from_json,
    records_to_json,
)
from fitness2garmin.keep_plan import build_keep_plan, summarize  # noqa: E402
from fitness2garmin.fit_builder import build_fit  # noqa: E402

MESG_FILE_ID = Profile["mesg_num"]["FILE_ID"]
MESG_SESSION = Profile["mesg_num"]["SESSION"]
MESG_SPORT = Profile["mesg_num"]["SPORT"]
_SPORT = {v: k for k, v in Profile["types"]["sport"].items()}
_SUB = {v: k for k, v in Profile["types"]["sub_sport"].items()}
_FILE = {v: k for k, v in Profile["types"]["file"].items()}
FIT_EPOCH_S = 631065600

UTC = timezone.utc
BASE = datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC)  # = 20:00 +08:00 local


def _summary_fit(sport: str, start_utc: datetime, dur_s: float) -> bytes:
    enc = Encoder()
    ts = int(start_utc.timestamp()) - FIT_EPOCH_S
    enc.write_mesg({
        "mesg_num": MESG_FILE_ID, "time_created": ts,
        "type": _FILE.get("activity"), "manufacturer": 10000,
    })
    enc.write_mesg({
        "mesg_num": MESG_SPORT, "sport": _SPORT.get(sport),
        "sub_sport": _SUB.get("generic"),
    })
    enc.write_mesg({
        "mesg_num": MESG_SESSION, "start_time": ts,
        "total_timer_time": dur_s, "total_elapsed_time": dur_s,
    })
    return enc.close()


def _add_row(ws, type_, dur, start, end, kcal, dist, avg, mx, hr):
    ws.append([type_, dur, start, end, kcal, dist, avg, mx, hr, "NULL"])


def make_fixture(td: Path) -> Path:
    """Six workouts, one per interesting matching case (see test_plan)."""
    src = td / "565994"
    src.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.title = "运动记录"
    ws.append(["运动类型", "运动时长(秒)", "开始时间", "结束时间", "卡路里",
               "运动距离(米)", "平均心率", "最大心率", "心率记录", "运动轨迹"])
    # 1 跑步 30min with HR — not in Garmin → upload
    s1 = BASE
    _add_row(ws, "跑步", 1800, "2025-03-01 20:00:00", "2025-03-01 20:30:00",
             300, 5000, 150, 170, json.dumps([140 + (i % 20) for i in range(600)]))
    # 2 跑步 twin pair at 2025-03-02: ghost 6h + real 40min; ghost has no Garmin
    #   twin either — the *real* one matches the garbage-duration Garmin entry.
    s2 = BASE + timedelta(days=1)
    _add_row(ws, "跑步", 2400, "2025-03-02 20:00:00", "2025-03-02 20:40:00",
             400, 7000, 155, 175, json.dumps([150 + (i % 15) for i in range(800)]))
    _add_row(ws, "跑步", 21600, "2025-03-02 20:00:00", "2025-03-03 02:00:00",
             0, 7000, 0, 0, "NULL")
    # 3 行走 15min — in Garmin (type-compatible, same duration) → skip
    s3 = BASE + timedelta(days=2)
    _add_row(ws, "行走", 900, "2025-03-03 08:00:00", "2025-03-03 08:15:00",
             50, 1000, 100, 120, "NULL")
    # 4 健身 20min — Apple recorded it as 'other' (same instant) → skip
    s4 = BASE + timedelta(days=3)
    _add_row(ws, "健身", 1200, "2025-03-04 21:00:00", "2025-03-04 21:20:00",
             150, 0, 0, 0, "NULL")
    # 5 健身 10s — junk fragment
    s5 = BASE + timedelta(days=4)
    _add_row(ws, "健身", 10, "2025-03-05 07:00:00", "2025-03-05 07:00:10",
             2, 0, 0, 0, "NULL")
    # 6 跑步机跑步 25min — in Garmin as treadmill_running → skip
    s6 = BASE + timedelta(days=5)
    _add_row(ws, "跑步", 1500, "2025-03-06 12:00:00", "2025-03-06 12:25:00",
             250, 4000, 140, 160, "NULL")
    wb.save(src / "565994.xlsx")

    entries = [
        ("1_跑步_20250301_203000.fit", _summary_fit("running", s1, 1800)),
        ("2_跑步_20250302_204000.fit", _summary_fit("running", s2, 2400)),
        ("3_跑步_20250303_020000.fit", _summary_fit("running", s2, 21600)),
        ("4_行走_20250303_081500.fit", _summary_fit("walking", s3, 900)),
        ("5_健身_20250304_212000.fit", _summary_fit("training", s4, 1200)),
        ("6_健身_20250305_070010.fit", _summary_fit("training", s5, 10)),
        ("7_跑步_20250306_122500.fit", _summary_fit("running", s6, 1500)),
    ]
    with zipfile.ZipFile(src / "bundle-565994.zip", "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return src


GARMIN = [
    # real twin of Keep #2 (garbage 18h duration, no GPS) → replace
    {"activityId": 101, "activityName": "Running", "startTimeGMT": "2025-03-02 12:00:00",
     "startTimeLocal": "2025-03-02 20:00:00", "duration": 64800.0, "distance": 7000,
     "hasPolyline": False, "activityType": {"typeKey": "running"}},
    # Keep #3 walking twin → skip
    {"activityId": 102, "activityName": "Walking", "startTimeGMT": "2025-03-03 12:00:00",
     "startTimeLocal": "2025-03-03 20:00:00", "duration": 900.0, "distance": 1000,
     "hasPolyline": True, "activityType": {"typeKey": "walking"}},
    # Keep #4 健身 recorded by Apple Watch as 'other' → skip
    {"activityId": 103, "activityName": "Other", "startTimeGMT": "2025-03-04 12:00:00",
     "startTimeLocal": "2025-03-04 20:00:00", "duration": 1200.0, "distance": 0,
     "hasPolyline": False, "activityType": {"typeKey": "other"}},
    # Keep #6 treadmill twin → skip
    {"activityId": 104, "activityName": "Treadmill", "startTimeGMT": "2025-03-06 12:00:00",
     "startTimeLocal": "2025-03-06 20:00:00", "duration": 1500.0, "distance": 4000,
     "hasPolyline": False, "activityType": {"typeKey": "treadmill_running"}},
    # garbage duration but HAS GPS → never replaced (Keep side unmatched → upload)
    {"activityId": 105, "activityName": "Running", "startTimeGMT": "2026-01-01 12:00:00",
     "startTimeLocal": "2026-01-01 20:00:00", "duration": 64800.0, "distance": 5000,
     "hasPolyline": True, "activityType": {"typeKey": "running"}},
]


def test_parse(records):
    assert len(records) == 7, len(records)
    r1 = records[0]
    assert r1.type == "跑步"
    assert r1.start_utc == BASE, r1.start_utc
    assert r1.duration_s == 1800
    assert r1.distance_m == 5000
    assert r1.kcal == 300
    assert r1.avg_hr == 150 and r1.max_hr == 170
    assert len(r1.hr) == 600 and r1.hr[0] == 140
    assert r1.fit_bytes is not None
    # all-zero HR series is dropped
    r2 = records[1]
    assert r2.hr and all(b > 0 for b in r2.hr)
    # 6th row junk has no HR
    assert records[5].hr == []


def test_json_roundtrip(records):
    again = records_from_json(records_to_json(records))
    assert len(again) == len(records)
    assert again[0].start_utc == records[0].start_utc
    assert again[0].hr == records[0].hr
    assert again[0].duration_s == records[0].duration_s


def test_plan(records):
    items = build_keep_plan(records, GARMIN)
    by_n = {it.n: it for it in items}
    assert by_n[1].action == "upload", by_n[1]
    assert by_n[2].action == "replace", by_n[2]
    assert by_n[2].garmin_id == 101
    assert "garbage duration" in by_n[2].reason
    assert by_n[3].action == "twin_ghost", by_n[3]   # 6h shell of #2
    assert by_n[4].action == "skip", by_n[4]          # walking twin
    assert by_n[4].garmin_id == 102
    assert by_n[5].action == "skip", by_n[5]          # 'other' twin via overlap
    assert by_n[5].garmin_id == 103
    assert by_n[6].action == "junk", by_n[6]
    assert by_n[7].action == "skip", by_n[7]          # treadmill_running
    counts = summarize(items)
    assert counts == {"upload": 1, "replace": 1, "skip": 3,
                      "junk": 1, "twin_ghost": 1}, counts


def test_fit(records):
    # #1 run with HR series → built FIT carries records, distance, calories
    data = build_fit(records[0].to_workout())
    msgs, errs = Decoder(Stream.from_byte_array(data)).read()
    assert not errs, errs
    sess = msgs["session_mesgs"][0]
    assert sess["sport"] == "running"
    assert sess["start_time"] == BASE
    assert abs(sess["total_timer_time"] - 1800) < 0.01
    assert abs(sess["total_distance"] - 5000) < 1
    assert sess["total_calories"] == 300
    assert sess["avg_heart_rate"] == 150 and sess["max_heart_rate"] == 170
    recs = msgs["record_mesgs"]
    assert len(recs) == 600
    hrs = [r["heart_rate"] for r in recs]
    assert hrs[0] == 140
    dists = [r["distance"] for r in recs]
    assert dists == sorted(dists)
    assert abs(dists[-1] - 5000) < 1
    # timestamps strictly increasing, inside the window
    ts = [r["timestamp"] for r in recs]
    assert all(a < b for a, b in zip(ts, ts[1:]))
    assert ts[0] >= BASE and ts[-1] <= BASE + timedelta(seconds=1800)
    # 健身 maps to strength-training sport family
    w = records[4].to_workout()  # n=5 健身
    assert w.activity_type == "HKWorkoutActivityTypeFunctionalStrengthTraining"


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = make_fixture(td)
        records = parse_keep_export(src)
        test_parse(records)
        print("[ok] keep parse")
        test_json_roundtrip(records)
        print("[ok] json roundtrip")
        test_plan(records)
        print("[ok] keep plan")
        test_fit(records)
        print("[ok] fit build")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
