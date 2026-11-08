"""Parse a Keep ( fitness app ) data export into workout records.

Expected layout (Keep's "数据导出" bundle):
    <dir>/<userid>.xlsx      sheet 运动记录, one row per workout
    <dir>/bundle-<userid>.zip  one summary-only .fit per workout,
                               named <n>_<sport-cn>_<end>_<time>.fit

The xlsx carries the rich fields (HR series, calories, distance); the bundle
FIT gives the authoritative UTC start instant (xlsx times are naive local).
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from garmin_fit_sdk import Decoder, Stream
from openpyxl import load_workbook

from .apple_export import HRSample, Workout

# Keep type -> HK activity type string used by fit_builder's sport map.
KEEP_TYPE_MAP = {
    "跑步": "HKWorkoutActivityTypeRunning",
    "行走": "HKWorkoutActivityTypeWalking",
    "骑行": "HKWorkoutActivityTypeCycling",
    "健身": "HKWorkoutActivityTypeFunctionalStrengthTraining",
}

_COLS = [
    "type", "dur", "start", "end", "kcal", "dist",
    "avg_hr", "max_hr", "hr_rec", "track",
]


@dataclass
class KeepRecord:
    n: int
    type: str  # 跑步 | 行走 | 骑行 | 健身
    start_local: datetime
    end_local: datetime
    duration_s: float
    distance_m: float
    kcal: float
    avg_hr: float | None
    max_hr: float | None
    hr: list[int] = field(default_factory=list)  # bpm samples, no timestamps
    start_utc: datetime | None = None  # aware, from the bundle FIT
    fit_bytes: bytes | None = None

    def to_json(self) -> dict:
        return {
            "n": self.n, "type": self.type,
            "start_local": self.start_local.isoformat(sep=" "),
            "end_local": self.end_local.isoformat(sep=" "),
            "duration_s": self.duration_s, "distance_m": self.distance_m,
            "kcal": self.kcal, "avg_hr": self.avg_hr, "max_hr": self.max_hr,
            "hr": self.hr,
            "start_utc": self.start_utc.isoformat() if self.start_utc else None,
        }

    @classmethod
    def from_json(cls, d: dict) -> "KeepRecord":
        return cls(
            n=d["n"], type=d["type"],
            start_local=datetime.strptime(d["start_local"], "%Y-%m-%d %H:%M:%S"),
            end_local=datetime.strptime(d["end_local"], "%Y-%m-%d %H:%M:%S"),
            duration_s=d["duration_s"], distance_m=d["distance_m"],
            kcal=d["kcal"], avg_hr=d["avg_hr"], max_hr=d["max_hr"],
            hr=d["hr"],
            start_utc=datetime.fromisoformat(d["start_utc"]) if d["start_utc"] else None,
        )

    def end_utc_as_utc(self) -> datetime:
        span = (self.end_local - self.start_local).total_seconds()
        return self.start_utc + timedelta(seconds=span)

    def to_workout(self) -> Workout:
        """Convert to an apple_export Workout on the UTC timeline.

        HR samples carry no timestamps in the export; they are spread evenly
        across the workout duration (same convention the pipeline uses for
        no-GPS Apple workouts). The span (end - start) is timezone-independent
        so deriving end from the FIT's UTC start is safe even when Keep's
        local clock was in another timezone than the export's +08:00 base.
        """
        start = self.start_utc
        span = (self.end_local - self.start_local).total_seconds()
        end = start + timedelta(seconds=span)
        hr = [
            HRSample(ts=start + timedelta(seconds=self.duration_s * i / max(len(self.hr) - 1, 1)),
                     bpm=float(bpm))
            for i, bpm in enumerate(self.hr)
        ]
        return Workout(
            activity_type=KEEP_TYPE_MAP.get(self.type, "HKWorkoutActivityTypeOther"),
            start=start,
            end=end,
            duration=self.duration_s,
            total_distance=self.distance_m / 1000.0,
            total_energy=self.kcal,
            source="keep",
            hr=hr,
            hr_avg=self.avg_hr if self.avg_hr and self.avg_hr > 0 else None,
            hr_max=self.max_hr if self.max_hr and self.max_hr > 0 else None,
        )


def _num(v, default=0.0) -> float:
    if v is None or v == "NULL":
        return default
    return float(v)


def parse_keep_export(export_dir: str | Path) -> list[KeepRecord]:
    export_dir = Path(export_dir)
    xlsx = next(p for p in export_dir.glob("*.xlsx") if not p.name.startswith("user"))
    bundle = next(p for p in export_dir.glob("bundle-*.zip"))

    wb = load_workbook(xlsx, read_only=True)
    ws = wb["运动记录"]
    records: list[KeepRecord] = []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=1):
        d = dict(zip(_COLS, row))
        hr_raw = d["hr_rec"]
        hr = []
        if hr_raw and hr_raw != "NULL":
            try:
                hr = [int(x) for x in json.loads(hr_raw)]
            except (json.JSONDecodeError, TypeError, ValueError):
                hr = []
        records.append(KeepRecord(
            n=i,
            type=str(d["type"]),
            start_local=datetime.strptime(str(d["start"]), "%Y-%m-%d %H:%M:%S"),
            end_local=datetime.strptime(str(d["end"]), "%Y-%m-%d %H:%M:%S"),
            duration_s=_num(d["dur"]),
            distance_m=_num(d["dist"]),
            kcal=_num(d["kcal"]),
            avg_hr=_num(d["avg_hr"], 0) or None,
            max_hr=_num(d["max_hr"], 0) or None,
            hr=[b for b in hr if b > 0],
        ))
    wb.close()

    # bundle FIT: authoritative UTC start + fallback bytes for no-HR workouts
    with zipfile.ZipFile(bundle) as z:
        for info in z.infolist():
            name = info.filename  # Keep sets the UTF-8 flag; already decoded
            parts = name.split("_", 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            n = int(parts[0])
            data = z.read(info)
            if not 1 <= n <= len(records):
                continue
            try:
                msgs, errs = Decoder(Stream.from_byte_array(data)).read()
                if errs:
                    continue
                start_utc = msgs["session_mesgs"][0]["start_time"]
            except Exception:  # noqa: BLE001 — summary FITs are optional
                continue
            rec = records[n - 1]
            rec.start_utc = start_utc
            rec.fit_bytes = data
    missing = [r.n for r in records if r.start_utc is None]
    if missing:
        raise SystemExit(f"[keep] no bundle FIT start time for rows: {missing[:10]}…")
    return records


def records_to_json(records: list[KeepRecord]) -> list[dict]:
    return [r.to_json() for r in records]


def records_from_json(items: list[dict]) -> list[KeepRecord]:
    return [KeepRecord.from_json(d) for d in items]
