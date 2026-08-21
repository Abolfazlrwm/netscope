"""
Local-first persistence.

Differentiator #9: privacy-first. Everything lives in a local SQLite
file by default; nothing is sent anywhere unless the user explicitly
opts into an export/report feature (reporting/evidence_report.py).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from netscope.core.models import RawMeasurement

DEFAULT_DB_PATH = Path.home() / ".netscope" / "netscope.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_type TEXT NOT NULL,
    target TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    packet_loss_pct REAL,
    jitter_ms REAL,
    error TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_target_time
    ON measurements(target, timestamp);
"""


class SqliteStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def save(self, measurement: RawMeasurement) -> None:
        self._conn.execute(
            """
            INSERT INTO measurements
                (probe_type, target, timestamp, success, latency_ms,
                 packet_loss_pct, jitter_ms, error, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                measurement.probe_type.value,
                measurement.target,
                measurement.timestamp.isoformat(),
                int(measurement.success),
                measurement.latency_ms,
                measurement.packet_loss_pct,
                measurement.jitter_ms,
                measurement.error,
                json.dumps(measurement.extra),
            ),
        )
        self._conn.commit()

    def recent(self, target: str, limit: int = 50) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.execute(
            "SELECT * FROM measurements WHERE target = ? ORDER BY timestamp DESC LIMIT ?",
            (target, limit),
        )
        return cur.fetchall()

    def close(self) -> None:
        self._conn.close()
