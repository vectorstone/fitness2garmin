"""SQLite ledger of what has been uploaded/deleted — makes the pipeline idempotent."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    workout_id TEXT PRIMARY KEY,
    fit_path TEXT,
    garmin_activity_id TEXT,
    status TEXT NOT NULL,          -- uploaded | duplicate | failed | deleted
    detail TEXT,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, db_path: str | Path):
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def mark(
        self,
        workout_id: str,
        status: str,
        fit_path: str | None = None,
        garmin_activity_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO uploads (workout_id, fit_path, garmin_activity_id, status, detail, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(workout_id) DO UPDATE SET
                status = excluded.status,
                fit_path = COALESCE(excluded.fit_path, uploads.fit_path),
                garmin_activity_id = COALESCE(excluded.garmin_activity_id, uploads.garmin_activity_id),
                detail = excluded.detail,
                updated_at = excluded.updated_at
            """,
            (workout_id, fit_path, garmin_activity_id, status, detail, _now()),
        )
        self.db.commit()

    def status_of(self, workout_id: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM uploads WHERE workout_id = ?", (workout_id,)
        ).fetchone()
        return row[0] if row else None

    def uploaded_ids(self) -> set[str]:
        rows = self.db.execute(
            "SELECT workout_id FROM uploads WHERE status IN ('uploaded', 'duplicate')"
        ).fetchall()
        return {r[0] for r in rows}

    def summary(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) FROM uploads GROUP BY status"
        ).fetchall()
        return {status: count for status, count in rows}
