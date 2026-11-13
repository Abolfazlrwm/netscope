"""
netscope.persistence.baseline_repository

SQLite-backed implementation of `core.ports.BaselineRepository` (the
port TASK-024 defined), making NetScope's headline differentiator --
personal, learned "what does normal look like for THIS network" --
actually persist across runs.

WHY THIS MATTERS
----------------
Until now every CLI run started from a fresh, empty `UserBaseline`
(`app/use_cases.py` documented this gap as "no BaselineRepository-backed
load/persist wiring exists yet"). Scoring and diagnosis still worked,
but the learned baseline never outlived a process: the tool could never
actually *learn*. This module closes that gap against the
`metric_baselines` table `persistence/schema.py` has carried since
TASK-029 -- the schema was designed for exactly this and simply had no
reader/writer.

STORED STATE (Welford-exact, not approximated)
----------------------------------------------
Each row stores `count`, `mean`, and `m2` -- Welford's online
mean/variance accumulator state (`core.baseline.MetricBaseline._m2`).
A `MetricBaseline` is reconstructed exactly from these three numbers;
`stddev` is *derived* (sqrt(m2 / (count - 1))) and never stored, so
round-tripping loses no statistical information. This mirrors
schema.py's own rationale for the table's column set.

UPSERT SEMANTICS
----------------
`save()` replaces the full baseline state in one transaction:
`INSERT OR REPLACE` per (target, metric_type) row, after deleting rows
that no longer exist in the in-memory baseline. The database is a
mirror of the passed-in `UserBaseline`, not an append log -- baseline
*observation* (Welford updates) happens in memory via
`UserBaseline.observe_*`, and `save()` persists the resulting state.
This keeps `save()` idempotent and makes load/save round-trips exact.

WHAT THIS MODULE DOES NOT DO
----------------------------
- Never observes new measurements itself -- persistence only. Turning a
  RawMeasurement into baseline observations is the app layer's job
  (app/use_cases.py), the same layering core/scoring.py and
  core/diagnosis.py already document.
- Never touches the `measurements` table -- that's
  `MeasurementRepository`'s aggregate.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from netscope.core.baseline import MetricBaseline, UserBaseline
from netscope.persistence.schema import create_connection

DEFAULT_DB_PATH = Path.home() / ".netscope" / "netscope.db"

_METRIC_TYPE_TO_STORE = {
    "latency": "latency",
    "packet_loss": "packet_loss",
}


def _row_to_metric_baseline(row: sqlite3.Row) -> MetricBaseline:
    """Reconstruct a MetricBaseline exactly from stored Welford state."""
    baseline = MetricBaseline()
    baseline.count = row["count"]
    baseline.mean = row["mean"]
    baseline._m2 = row["m2"]
    return baseline


class BaselineRepository:
    """Loads and saves the single local `UserBaseline` in SQLite,
    exposing only `core.baseline` types -- never `sqlite3.Row` or SQL
    results. Satisfies the `core.ports.BaselineRepository` Protocol
    structurally (save/load); no import or subclass needed.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        # Every repository access is a short, self-contained transaction
        # (the v0.7 cross-thread doctrine); the lock makes "short" also
        # EXCLUSIVE -- see ServiceRepository's own comment for the
        # statement-cache race this closes, and schema.py for the story.
        self._lock = threading.Lock()

    @classmethod
    def open(cls, db_path: Path | str = DEFAULT_DB_PATH) -> BaselineRepository:
        """Open (creating and initializing if needed) a database file
        and return a repository backed by it -- delegates to
        `persistence.schema.create_connection`, the one canonical
        schema-initialization path (TASK-029)."""
        return cls(create_connection(db_path))

    def load(self) -> UserBaseline:
        """Load the persisted baseline. Returns a fresh, empty
        `UserBaseline` when nothing has been saved yet -- a missing
        baseline is the normal first-run state, not an error (the same
        convention the port's own docstring establishes)."""
        with self._lock:
            baseline = UserBaseline()
            cur = self._conn.execute("SELECT target, metric_type, count, mean, m2 FROM metric_baselines")
            for row in cur.fetchall():
                store_name = _METRIC_TYPE_TO_STORE[row["metric_type"]]
                getattr(baseline, store_name)[row["target"]] = _row_to_metric_baseline(row)
            cur = self._conn.execute("SELECT target, count, mean, m2 FROM throughput_baselines")
            for row in cur.fetchall():
                baseline.throughput[row["target"]] = _row_to_metric_baseline(row)
            cur = self._conn.execute("SELECT target, count, mean, m2 FROM jitter_baselines")
            for row in cur.fetchall():
                baseline.jitter[row["target"]] = _row_to_metric_baseline(row)
            return baseline

    def save(self, baseline: UserBaseline) -> None:
        """Persist the full baseline state in one transaction: replace
        every (target, metric_type) row -- and every throughput and
        jitter row -- so the database mirrors the in-memory object
        exactly."""
        rows: list[tuple[str, str, int, float, float]] = []
        for metric_type, store in (("latency", baseline.latency), ("packet_loss", baseline.packet_loss)):
            for target, metric in store.items():
                rows.append((target, metric_type, metric.count, metric.mean, metric._m2))
        throughput_rows: list[tuple[str, int, float, float]] = [
            (target, metric.count, metric.mean, metric._m2) for target, metric in baseline.throughput.items()
        ]
        jitter_rows: list[tuple[str, int, float, float]] = [
            (target, metric.count, metric.mean, metric._m2) for target, metric in baseline.jitter.items()
        ]

        with self._lock, self._conn:
            self._conn.execute("DELETE FROM metric_baselines")
            self._conn.executemany(
                "INSERT OR REPLACE INTO metric_baselines (target, metric_type, count, mean, m2) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.execute("DELETE FROM throughput_baselines")
            self._conn.executemany(
                "INSERT OR REPLACE INTO throughput_baselines (target, count, mean, m2) VALUES (?, ?, ?, ?)",
                throughput_rows,
            )
            self._conn.execute("DELETE FROM jitter_baselines")
            self._conn.executemany(
                "INSERT OR REPLACE INTO jitter_baselines (target, count, mean, m2) VALUES (?, ?, ?, ?)",
                jitter_rows,
            )

    def close(self) -> None:
        self._conn.close()
