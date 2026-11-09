"""
netscope.persistence.service_repository

ServiceRepository -- the curated service list's SQLite home (TASK-032's
storage half, v0.7), implementing the `ServiceRepository` port
`core/ports.py` defines (ADR-005's second repository port to land, after
TASK-024's BaselineRepository).

WHY THIS LIVES IN persistence, NOT core
-------------------------------------------
`sqlite3` is infrastructure (adr-001-architecture-style.md,
module-boundaries.md's Persistence section). `core.models.Service`
defines what a curated service *is* -- "data someone supplied," never a
name baked into logic; this module defines how that supplied data is
stored and retrieved. `core` never imports this module or `sqlite3`.

WHAT THE CONTRACT PROMISES (core/ports.py)
--------------------------------------------
- `save()` is an upsert keyed by `service.name`: saving a Service whose
  name already exists replaces that entry's host and checks in place.
  Implemented with `INSERT ... ON CONFLICT DO UPDATE` rather than
  `INSERT OR REPLACE`: REPLACE deletes-then-inserts the whole row, which
  would silently reset any column a future migration adds; DO UPDATE
  touches exactly the two columns a Service actually carries.
- `load_all()` returns every saved Service, alphabetical by name. An
  empty list (nothing curated yet) is a normal result, not an error --
  v0.6's services screen already treats a service-less board as the
  normal first-run state.
- `remove(name)` deletes one entry and returns whether a row actually
  existed, so callers say "removed" versus "no such service" honestly.

CHECKS SERIALIZATION
-----------------------
`Service.enabled_checks` (a `set[ProbeType]`) is stored as one
comma-separated `services.checks` string, sorted alphabetically by the
ProbeType value ('' when none are enabled -- "each optional" per the
Service model). Sorting at the storage boundary is deliberate: a Python
set has no stable iteration order, and the same Service saved twice must
produce byte-identical rows -- otherwise an upsert that changed nothing
would still rewrite the row, and two readers could disagree on what
"the same list" means. Display order is the UI's business
(`ui/tui/screens/services.py` keeps its own fixed column order); storage
never pretends to know it.

The parse side is strict, matching `measurement_repository.py`'s own
row-mapping convention: an unknown ProbeType value in a stored row
raises ValueError from the enum constructor. A corrupted row is a bug
worth surfacing, not silently skipping -- quietly dropping one of the
user's saved services would be worse than failing loudly.

WHAT THIS MODULE DOES NOT DO
-----------------------------
No DDL (the `services` table belongs to `persistence/schema.py`, the one
canonical schema-initialization path), no check-running (that is
`app/use_cases.py`'s `run_service_checks`, unchanged since TASK-033),
and no curated-list defaults: ADR-005's "no hardcoded providers" rule
means this repository ships empty and stays exactly as empty as the
user left it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from netscope.core.models import ProbeType, Service
from netscope.persistence.schema import create_connection

DEFAULT_DB_PATH = Path.home() / ".netscope" / "netscope.db"


def _checks_to_storage(enabled_checks: set[ProbeType]) -> str:
    """The one place checks are serialized: comma-separated, sorted by
    the ProbeType value, '' when empty. See the module docstring for why
    the sort is part of the contract, not cosmetics."""
    return ",".join(sorted(check.value for check in enabled_checks))


def _storage_to_checks(stored: str) -> set[ProbeType]:
    """The one place checks are deserialized. Strict: an unknown value
    raises from the ProbeType constructor rather than being skipped."""
    return {ProbeType(value) for value in stored.split(",") if value}


def _row_to_service(row: sqlite3.Row) -> Service:
    """The one place a sqlite3.Row is touched -- converted immediately,
    never returned to a caller (the same boundary rule
    measurement_repository.py established)."""
    return Service(
        name=row["name"],
        host=row["host"],
        enabled_checks=_storage_to_checks(row["checks"]),
    )


class ServiceRepository:
    """Stores and retrieves the curated service list in SQLite,
    exposing only `core.models` types -- never `sqlite3.Row` or raw SQL
    results."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, db_path: Path | str = DEFAULT_DB_PATH) -> ServiceRepository:
        """Open (creating and initializing if needed) a database file
        and return a repository backed by it -- delegates entirely to
        `persistence.schema.create_connection`, the one canonical
        schema-initialization path (TASK-029). A v2-era file upgrades
        additively on open (the v3 `services` table appears alongside
        every pre-existing row)."""
        return cls(create_connection(db_path))

    def save(self, service: Service) -> None:
        """Upsert keyed by `service.name` (see the module docstring for
        why DO UPDATE and not INSERT OR REPLACE). Wrapped in a
        transaction (`with self._conn:`) like every other write in this
        package."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO services (name, host, checks)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    host = excluded.host,
                    checks = excluded.checks
                """,
                (service.name, service.host, _checks_to_storage(service.enabled_checks)),
            )

    def load_all(self) -> list[Service]:
        """Every saved Service, alphabetical by name -- the port's
        documented deterministic order."""
        cur = self._conn.execute("SELECT * FROM services ORDER BY name ASC")
        return [_row_to_service(row) for row in cur.fetchall()]

    def get(self, name: str) -> Service | None:
        """One saved Service by exact name, or None -- a convenience the
        port doesn't require but every caller (the CLI's add/remove/
        check, the TUI's save flow) ends up wanting."""
        cur = self._conn.execute("SELECT * FROM services WHERE name = ?", (name,))
        row = cur.fetchone()
        return _row_to_service(row) if row is not None else None

    def remove(self, name: str) -> bool:
        """Delete one saved Service by exact name; True iff a row
        actually existed."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM services WHERE name = ?", (name,))
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
