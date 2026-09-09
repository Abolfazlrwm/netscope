"""
netscope.persistence.measurement_repository

MeasurementRepository (TASK-030), the successor to
`persistence/sqlite_store.py`'s `SqliteStore`, per
`future-roadmap.md`'s TASK-030 row: "Implement MeasurementRepository,
migrating today's sqlite_store.py logic and fixing the sqlite3.Row-
leakage issue flagged in module-boundaries.md."

SCOPE NOTE -- WHY THIS TASK IS MEASUREMENT-ONLY
----------------------------------------------------
This turn's instructions described a much broader TASK-030 (Measurement
+ Route + Baseline + Incident repositories together). The actual
authoritative `future-roadmap.md` row for TASK-030 scopes it to exactly
one file, `persistence/measurement_repository.py`, migrating exactly
`sqlite_store.py` -- Route/Baseline/Incident repositories are not
mentioned in that row at all. `RouteRepository`/`IncidentRepository`
don't have ports in `core/ports.py` yet either (only `BaselineRepository`
does, from TASK-024, itself still unimplemented against SQLite). Per
this same turn's own instruction ("do NOT blindly create all of these
if the actual repository architecture says otherwise... use the
existing ADR/schema/domain model state as the authority") and this
project's established practice of following the documented roadmap
over a broader restatement when the two disagree, this task implements
`MeasurementRepository` only. The other three remain for their own,
separately-scoped future tasks.

WHY THIS LIVES IN persistence, NOT core
-------------------------------------------
`sqlite3` is infrastructure (adr-001-architecture-style.md,
module-boundaries.md's Persistence section: "Dependencies: core.models
..., stdlib sqlite3"). `core` defines what a measurement *is*
(`core.models.RawMeasurement`); this module defines how one is stored
and retrieved. `core` never imports this module or `sqlite3`.

WHAT THIS MODULE FIXES
---------------------------
module-boundaries.md's Persistence section flags a concrete, audited
gap in `sqlite_store.py`: "`recent()` returns `sqlite3.Row` objects
directly... a gap in today's audited `sqlite_store.py`... the future
repository implementation must fix, not preserve." `recent()` here
returns `list[RawMeasurement]` -- `sqlite3.Row` never crosses this
module's public boundary; it is used internally as a convenience and
converted before any method returns.

SCHEMA / CONNECTION REUSE
-----------------------------
This module does not define or duplicate any DDL. It depends entirely
on `persistence.schema` (TASK-029) for both the `measurements` table
definition and database initialization (`create_connection`) -- there
remains exactly one canonical schema-initialization path.

HISTORY QUERIES (TASK-031)
-------------------------------
`history()` extends `save`/`recent` with target/time-range/probe-type
filtering, per future-roadmap.md's TASK-031 row ("Implement time-range/
target-filtered queries needed by CLI's `history` command"). Per that
task's own "avoid having two unrelated SQL implementations that drift"
guidance, `recent()` is now a thin wrapper around `history()` rather
than a second, independent query -- there is exactly one place that
builds a `measurements` SELECT and one place (`_row_to_measurement`)
that converts a row back to a `RawMeasurement`.

Ordering: both `history()` and `recent()` order `ORDER BY timestamp
DESC` (newest first) -- this is `recent()`'s own pre-existing
convention (TASK-030), kept as the one shared, documented default
rather than introducing a second, inconsistent order for what is
otherwise the same underlying query.

Boundary semantics: `start`/`end` are both **inclusive**
(`timestamp >= start AND timestamp <= end`) -- a measurement landing
exactly on either boundary is included, not excluded.

Limit validation: `limit=None` means unlimited (no SQL `LIMIT` clause
at all, returning every matching row). `limit <= 0` raises `ValueError`
explicitly rather than silently returning an empty list or being
ignored -- an invalid limit is a caller bug, not a valid query for
"nothing"/"everything".

Timestamp comparison: `start`/`end` are compared as the same
`.isoformat()` string form `save()` already writes (TASK-030's
existing convention, unchanged) -- this relies on every stored
timestamp using the same fixed-width, fixed-offset ISO 8601 form
(guaranteed here since every `RawMeasurement.timestamp` defaults to
`utcnow()`, always `+00:00`), under which lexicographic string
ordering and chronological ordering coincide. This is not a new
assumption introduced by this task -- `recent()`'s pre-existing
`ORDER BY timestamp DESC` already relied on exactly this.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement
from netscope.persistence.schema import create_connection

DEFAULT_DB_PATH = Path.home() / ".netscope" / "netscope.db"


def _row_to_measurement(row: sqlite3.Row) -> RawMeasurement:
    """The one place a sqlite3.Row is touched -- converted immediately,
    never returned to a caller."""
    return RawMeasurement(
        probe_type=ProbeType(row["probe_type"]),
        target=row["target"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        success=bool(row["success"]),
        latency_ms=row["latency_ms"],
        packet_loss_pct=row["packet_loss_pct"],
        jitter_ms=row["jitter_ms"],
        error=row["error"],
        error_type=ProbeErrorType(row["error_type"]) if row["error_type"] is not None else None,
        extra=json.loads(row["extra_json"]) if row["extra_json"] is not None else {},
    )


class MeasurementRepository:
    """Stores and retrieves `RawMeasurement` objects in SQLite, exposing
    only `core.models` types -- never `sqlite3.Row` or raw SQL results.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, db_path: Path | str = DEFAULT_DB_PATH) -> "MeasurementRepository":
        """Open (creating and initializing if needed) a database file
        and return a repository backed by it -- delegates entirely to
        `persistence.schema.create_connection`, the one canonical
        schema-initialization path (TASK-029)."""
        return cls(create_connection(db_path))

    def save(self, measurement: RawMeasurement) -> None:
        """Persist one measurement. Wrapped in a transaction (`with
        self._conn:`) so a failure partway through never leaves a
        half-written row -- a single-statement INSERT is already
        atomic in SQLite, but this makes that guarantee explicit rather
        than incidental.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO measurements
                    (probe_type, target, timestamp, success, latency_ms,
                     packet_loss_pct, jitter_ms, error, error_type, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    measurement.error_type.value if measurement.error_type is not None else None,
                    json.dumps(measurement.extra),
                ),
            )

    def history(
        self,
        target: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        probe_type: Optional[ProbeType] = None,
        limit: Optional[int] = None,
    ) -> list[RawMeasurement]:
        """Historical measurements for `target`, newest first, optionally
        filtered by an inclusive `[start, end]` timestamp range and/or a
        specific `probe_type`. `limit=None` returns every matching row;
        `limit` must be a positive integer otherwise (see module
        docstring for the full boundary/ordering/limit semantics this
        task establishes).

        Always returns `RawMeasurement` objects -- never `sqlite3.Row`.
        """
        if limit is not None and limit <= 0:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")

        query = "SELECT * FROM measurements WHERE target = ?"
        params: list = [target]

        if start is not None:
            query += " AND timestamp >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND timestamp <= ?"
            params.append(end.isoformat())
        if probe_type is not None:
            query += " AND probe_type = ?"
            params.append(probe_type.value)

        query += " ORDER BY timestamp DESC"

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cur = self._conn.execute(query, params)
        return [_row_to_measurement(row) for row in cur.fetchall()]

    def recent(self, target: str, limit: int = 50) -> list[RawMeasurement]:
        """The most recent `limit` measurements for `target`. A thin,
        backward-compatible wrapper around `history()` -- see that
        method for the shared query/ordering/row-mapping logic.
        """
        return self.history(target, limit=limit)

    def close(self) -> None:
        self._conn.close()
