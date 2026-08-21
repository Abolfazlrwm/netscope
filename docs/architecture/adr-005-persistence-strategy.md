# ADR-005 — Persistence Strategy

**Status:** Accepted

## Context

The current MVP's `persistence/sqlite_store.py` (audited in TASK-002) is
genuinely local-first and correct for what it stores, but couples callers
directly to a concrete `SqliteStore` class and has schema for `measurements`
only — no tables for baselines, routes, services, or incidents, even though
`core.models` already defines some of those shapes.

## Decision

**Keep SQLite** as the storage engine — it requires no server process, ships
in the Python standard library (`sqlite3`, no new dependency), and is
appropriate for a single-user, local-first tool exactly as Phase 1 research
concluded. This is not revisited; what changes is how it's accessed.

**Introduce repository ports in `core`**, one per aggregate:
`MeasurementRepository`, `RouteRepository`, `BaselineRepository`,
`ServiceRepository`, `IncidentRepository` (incidents together with the
diagnoses/evidence attached to them, since an `Incident` owns its `Diagnosis`
per architecture-overview.md §5). Each is a small `Protocol` (e.g. `save(...)`,
`recent(...)`, `get(...)`) defined in `core.ports`, mirroring the `Probe`
pattern from ADR-002 — `core`/`app` depend on these interfaces; `persistence`
implements them.

**`persistence/` owns the SQLite implementation exclusively.** `core` and
`app`'s use cases never import `sqlite3`. Today's schema (the `measurements`
table) becomes the first table of a larger, still-SQLite-backed schema; no
migration mechanism or exact schema is designed in this task, per the task's
explicit instruction not to design migrations yet.

## Alternatives considered

- **An embedded document store (e.g. a JSON-lines file per table).** Rejected:
  SQLite already gives transactional writes and indexed queries (today's
  `idx_measurements_target_time` index, confirmed present in the audited code)
  for free, which a flat-file approach would have to reimplement.
- **A local server-based database (e.g. embedded Postgres).** Rejected: adds
  an operational dependency (a running server process) that contradicts the
  "no cloud/server infrastructure required" principle from
  architecture-overview.md §14, for no benefit at this project's scale.
- **An ORM (e.g. SQLAlchemy).** Not rejected outright, but not decided here
  either — it's an implementation detail *inside* `persistence`, invisible to
  `core`/`app` behind the repository ports, so it doesn't need an ADR of its
  own. Whoever implements `persistence` can choose raw `sqlite3` (as today) or
  an ORM without this decision needing to change.

## Consequences

- `core`/`app` can be fully unit-tested against fake repositories with no
  SQLite file at all, extending the same testability property `core` already
  has (per TASK-003's 51 offline tests).
- SQLite can be swapped for something else later by reimplementing the
  repository ports only — nothing outside `persistence/` would need to change,
  answering "Can SQLite be replaced later?" (yes) from
  architecture-overview.md §16.
- Today's `sqlite_store.py` is not thrown away — its schema and query logic
  become the starting point for `persistence/`'s `MeasurementRepository`
  implementation when that migration happens.
