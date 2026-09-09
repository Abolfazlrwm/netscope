"""
netscope.persistence.schema

SQLite schema foundation (TASK-029), per ADR-005 ("Introduce repository
ports in core... persistence/ owns the SQLite implementation exclusively.
Today's schema (the measurements table) becomes the first table of a
larger, still-SQLite-backed schema; no migration mechanism or exact
schema is designed in this task [ADR-005/TASK-024], per that task's
explicit instruction not to design migrations yet.") and
module-boundaries.md's Persistence section ("Own schema, indices, and
any future migration mechanism entirely.").

This module owns exactly two responsibilities:
    1. The schema DDL itself (what tables/columns/indices/foreign keys
       exist), for the five aggregates ADR-005 names:
       MeasurementRepository, RouteRepository, BaselineRepository,
       IncidentRepository (incidents together with the diagnoses/
       evidence attached to them, per ADR-005) -- and NOT
       ServiceRepository, see "WHAT'S DELIBERATELY NOT HERE" below.
    2. Schema lifecycle: initializing a fresh database, and reporting
       the current schema version -- deterministically, idempotently,
       and without ever discarding existing data.

WHAT THIS MODULE DOES *NOT* DO
---------------------------------
Per this task's explicit scope, and ADR-005's "no migration mechanism
or exact schema is designed [yet]" becoming exactly this task's job to
finally do -- this module does NOT implement repository classes, save/
query methods, or any read/write logic beyond schema creation itself.
`persistence/sqlite_store.py`'s existing `SqliteStore.save`/`.recent()`
are left exactly as they are (including the `sqlite3.Row`-leaking gap
module-boundaries.md's Persistence section already flags as "a concrete
thing the future repository implementation must fix, not preserve") --
that migration is TASK-030's job, not this one's. The only change made
to `sqlite_store.py` here is pointing its existing schema-creation call
at this module's `SCHEMA` instead of its own separate copy of the
`measurements` table DDL, so there is exactly one canonical definition
of that table rather than two silently-drifting ones.

SCHEMA VERSIONING
---------------------
Uses SQLite's own built-in `PRAGMA user_version` -- an integer stored in
the database file's header, present in every SQLite database for
exactly this purpose. No new `schema_version` table, no third-party
migration framework: "a minimal, explicit schema-version mechanism is
preferred" is satisfied by using the mechanism SQLite already ships
with, adding zero new dependencies and zero new tables. `initialize_database()`
is fully idempotent: every statement in `SCHEMA` is `CREATE TABLE/INDEX
IF NOT EXISTS`, and re-setting `PRAGMA user_version` to the same
`SCHEMA_VERSION` on an already-initialized database is a no-op with
respect to existing rows -- nothing is ever dropped or truncated.

TABLE DESIGN RATIONALE
--------------------------
Every table below maps directly to an existing `core.models`/
`core.baseline` dataclass already in the repository -- none is
speculative:

- `measurements`: RawMeasurement. The pre-existing table from today's
  `sqlite_store.py`, kept as the first table per ADR-005, with one
  genuine (non-speculative) gap fixed: `RawMeasurement.error_type`
  (added by an earlier task) had no corresponding column.
- `route_snapshots` / `route_hops`: RouteSnapshot / RouteHop. Hops are
  an ordered list on the Python side, so `route_hops` carries an
  explicit `hop_index` -- SQL rows have no inherent order.
- `metric_baselines`: UserBaseline's `latency`/`packet_loss` dicts of
  MetricBaseline. Stores `count`/`mean`/`m2` (not `stddev`, which is
  *derived* from `m2` and `count` by MetricBaseline.stddev's own
  property, per Welford's algorithm) so a MetricBaseline can be
  reconstructed exactly, not approximated.
- `evidence`: Evidence. `source` (`Optional[RawMeasurement |
  RouteSnapshot]`) becomes two nullable foreign keys
  (`source_measurement_id`, `source_route_snapshot_id`) -- at most one
  populated at a time, directly reflecting the Python union type,
  without a generic polymorphic join table.
- `diagnoses` / `diagnosis_evidence`: Diagnosis. `evidence: list[Evidence]`
  is a genuine many-to-many relationship (the same Evidence object could
  in principle back more than one Diagnosis), so it gets its own join
  table rather than a denormalized column.
- `diagnosis_ruled_out`: Diagnosis.ruled_out: list[Evidence | Hypothesis].
  Each row is either an Evidence reference or a bare Hypothesis value,
  never both -- enforced with a CHECK constraint mirroring the Python
  union.
- `incidents` / `incident_evidence`: Incident. `incident_evidence` is
  deliberately a *separate* join table from `diagnosis_evidence`:
  `Incident.evidence` is documented (core/models.py's own Incident
  docstring, TASK-009) as "the union of evidence across the incident's
  lifetime, not just its start" -- a different, accumulating set from
  any single Diagnosis's own evidence list, so conflating the two join
  tables would lose that distinction.

WHAT'S DELIBERATELY NOT HERE
--------------------------------
No `services` table. ADR-005 names `ServiceRepository` as one of the
five expected ports, but the `Service` domain model itself does not
exist yet in `core.models` -- it's TASK-032's own, separately-scoped
future task (future-roadmap.md Phase G). Designing a schema for a type
that doesn't exist yet would be exactly the kind of speculative,
guessed-at column set this task's own scope explicitly warns against;
this is deferred for the same reason TASK-023 (GeoIP) was deferred
earlier in this project's history -- a genuine, documented prerequisite
gap, not an oversight.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

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
    error_type TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_target_time
    ON measurements(target, timestamp);

CREATE TABLE IF NOT EXISTS route_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_snapshots_target_time
    ON route_snapshots(target, timestamp);

CREATE TABLE IF NOT EXISTS route_hops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_snapshot_id INTEGER NOT NULL REFERENCES route_snapshots(id),
    hop_index INTEGER NOT NULL,
    ttl INTEGER NOT NULL,
    address TEXT,
    hostname TEXT,
    avg_rtt_ms REAL,
    packet_loss_pct REAL NOT NULL,
    asn TEXT,
    organization TEXT,
    country TEXT,
    is_unstable INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_route_hops_snapshot
    ON route_hops(route_snapshot_id, hop_index);

CREATE TABLE IF NOT EXISTS metric_baselines (
    target TEXT NOT NULL,
    metric_type TEXT NOT NULL CHECK (metric_type IN ('latency', 'packet_loss')),
    count INTEGER NOT NULL,
    mean REAL NOT NULL,
    m2 REAL NOT NULL,
    PRIMARY KEY (target, metric_type)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL,
    tested INTEGER NOT NULL DEFAULT 1,
    observed_value REAL,
    expected_value REAL,
    deviation REAL,
    severity TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_measurement_id INTEGER REFERENCES measurements(id),
    source_route_snapshot_id INTEGER REFERENCES route_snapshots(id),
    CHECK (source_measurement_id IS NULL OR source_route_snapshot_id IS NULL)
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnosis_evidence (
    diagnosis_id INTEGER NOT NULL REFERENCES diagnoses(id),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    PRIMARY KEY (diagnosis_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS diagnosis_ruled_out (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnosis_id INTEGER NOT NULL REFERENCES diagnoses(id),
    evidence_id INTEGER REFERENCES evidence(id),
    hypothesis TEXT,
    CHECK (
        (evidence_id IS NOT NULL AND hypothesis IS NULL)
        OR (evidence_id IS NULL AND hypothesis IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    severity TEXT,
    target TEXT,
    diagnosis_id INTEGER REFERENCES diagnoses(id)
);
CREATE INDEX IF NOT EXISTS idx_incidents_target_started
    ON incidents(target, started_at);

CREATE TABLE IF NOT EXISTS incident_evidence (
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    PRIMARY KEY (incident_id, evidence_id)
);
"""


def initialize_database(conn: sqlite3.Connection) -> None:
    """Create every table/index in SCHEMA if it doesn't already exist,
    enable foreign-key enforcement, and record SCHEMA_VERSION.

    Safe to call on every startup and in every test: every DDL statement
    is `IF NOT EXISTS`, so calling this on an already-initialized
    database with existing rows is a no-op as far as those rows are
    concerned -- nothing is dropped, truncated, or recreated.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """The schema version currently recorded in this database (0 for a
    brand-new, never-initialized SQLite file -- SQLite's own default for
    `user_version`)."""
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return version


def create_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating the parent directory and file if needed) and
    initialize a database in one step -- the same pattern
    `SqliteStore.__init__` already used for the `measurements`-only
    schema, now delegating to this module's full schema instead of its
    own separate copy.
    """
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    initialize_database(conn)
    return conn
