"""
netscope.persistence.route_repository

RouteRepository -- the route history's SQLite home (v0.8), implementing
the `RouteRepository` port `core/ports.py` defines (ADR-005's third
repository port to land, after TASK-024's BaselineRepository and v0.7's
ServiceRepository).

THE TABLES WERE ALWAYS HERE
-----------------------------
`route_snapshots`/`route_hops` have existed since schema v1 -- ADR-005
drew them in the very first schema, TASK-029 created them on every
database it initialized -- and until v0.8 they were dead weight: no
writer, no reader, `netscope route` computed its churn verdict over the
single in-memory snapshot it had just produced and discarded the path.
This module is that story's payoff: the tables get their writer WITHOUT
a schema migration -- SCHEMA_VERSION stays 3, a v2-era or v3-era file
opens as-is, and the first `netscope route` run starts filling tables
that have been waiting since day one.

WHAT THE CONTRACT PROMISES (core/ports.py)
--------------------------------------------
- `save()` **appends** one observation: routes are a log, not a curated
  list. One transaction writes the `route_snapshots` row and all of its
  `route_hops` children -- a traceroute that dies halfway through the
  hop INSERTs leaves no snapshot row behind at all, never a headless
  one (the same never-a-half-write rule every repository in this
  package wraps its writes in).
- `load_recent(target, limit)` returns the target's most recent
  `limit` snapshots **oldest first** -- the exact chronological
  sequence `core.routing.analyze_route_churn` requires (it compares
  consecutive pairs and deliberately does not sort; feeding it
  newest-first would invert every change's direction). The newest-first
  selection runs on `(timestamp DESC, id DESC)` -- the timestamp column
  follows measurement_repository.py's own fixed-width, fixed-offset
  ISO-8601 convention (every `RouteSnapshot.timestamp` defaults to
  `utcnow()`, always `+00:00`), under which lexicographic and
  chronological ordering coincide; `id DESC` breaks ties when two runs
  share a timestamp, so the newest INSERT wins rather than an arbitrary
  row.

NO DDL HERE
-------------
The tables belong to `persistence/schema.py`, the one canonical
schema-initialization path (TASK-029) -- this module only ever INSERTs
into and SELECTs from them, like every other repository in this
package. `create_connection` guarantees the tables exist before any
repository method can run.

WHAT THIS MODULE DOES NOT DO
-----------------------------
No churn analysis (that is `core.routing.analyze_route_churn`, given
the loaded sequence by `app/use_cases.py`'s `run_route_analysis`),
no pruning of old snapshots (the window a run compares against is the
use case's business; this module stores what it is told to store), and
no upsert semantics -- every save is a new row, because a route
observation is append-only evidence.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from netscope.core.models import RouteHop, RouteSnapshot
from netscope.persistence.schema import create_connection

DEFAULT_DB_PATH = Path.home() / ".netscope" / "netscope.db"


def _row_to_hop(row: sqlite3.Row) -> RouteHop:
    """The one place a route_hops row is touched -- converted
    immediately, never returned to a caller (the same boundary rule
    measurement_repository.py established)."""
    return RouteHop(
        ttl=row["ttl"],
        address=row["address"],
        hostname=row["hostname"],
        avg_rtt_ms=row["avg_rtt_ms"],
        packet_loss_pct=row["packet_loss_pct"],
        asn=row["asn"],
        organization=row["organization"],
        country=row["country"],
        is_unstable=bool(row["is_unstable"]),
    )


class RouteRepository:
    """Stores and retrieves `RouteSnapshot` objects in SQLite, exposing
    only `core.models` types -- never `sqlite3.Row` or raw SQL
    results."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        # Every repository access is a short, self-contained transaction
        # (the v0.7 cross-thread doctrine); the lock makes "short" also
        # EXCLUSIVE -- see ServiceRepository's own comment for the
        # statement-cache race this closes, and schema.py for the story.
        self._lock = threading.Lock()

    @classmethod
    def open(cls, db_path: Path | str = DEFAULT_DB_PATH) -> RouteRepository:
        """Open (creating and initializing if needed) a database file
        and return a repository backed by it -- delegates entirely to
        `persistence.schema.create_connection`, the one canonical
        schema-initialization path (TASK-029). No migration is involved:
        the route tables have existed since schema v1."""
        return cls(create_connection(db_path))

    def save(self, snapshot: RouteSnapshot) -> None:
        """Append one observation: a `route_snapshots` row plus one
        `route_hops` row per hop, all in a single transaction (see the
        module docstring for why the snapshot row must not outlive a
        failed hop write)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                    INSERT INTO route_snapshots (target, timestamp)
                    VALUES (?, ?)
                    """,
                (snapshot.target, snapshot.timestamp.isoformat()),
            )
            snapshot_id = cur.lastrowid
            self._conn.executemany(
                """
                    INSERT INTO route_hops
                        (route_snapshot_id, hop_index, ttl, address, hostname,
                         avg_rtt_ms, packet_loss_pct, asn, organization, country,
                         is_unstable)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                [
                    (
                        snapshot_id,
                        hop_index,
                        hop.ttl,
                        hop.address,
                        hop.hostname,
                        hop.avg_rtt_ms,
                        hop.packet_loss_pct,
                        hop.asn,
                        hop.organization,
                        hop.country,
                        int(hop.is_unstable),
                    )
                    for hop_index, hop in enumerate(snapshot.hops)
                ],
            )

    def load_recent(self, target: str, limit: int = 20) -> list[RouteSnapshot]:
        """The target's most recent `limit` snapshots, **oldest first**
        -- the port's documented chronological order (see the module
        docstring for the ordering/tie-break conventions). Fewer than
        `limit` snapshots, or none at all, is a normal result."""
        if limit <= 0:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")

        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM route_snapshots
                WHERE target = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (target, limit),
            )
            rows = list(reversed(cur.fetchall()))  # fetched newest first; hand back oldest first

            hops_by_snapshot: dict[int, list[RouteHop]] = {}
            if rows:
                snapshot_ids = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in snapshot_ids)
                hops_cur = self._conn.execute(
                    f"""
                    SELECT * FROM route_hops
                    WHERE route_snapshot_id IN ({placeholders})
                    ORDER BY route_snapshot_id ASC, hop_index ASC
                    """,
                    snapshot_ids,
                )
                for hop_row in hops_cur.fetchall():
                    hops_by_snapshot.setdefault(hop_row["route_snapshot_id"], []).append(_row_to_hop(hop_row))

            return [
                RouteSnapshot(
                    target=row["target"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    hops=hops_by_snapshot.get(row["id"], []),
                )
                for row in rows
            ]

    def close(self) -> None:
        self._conn.close()
