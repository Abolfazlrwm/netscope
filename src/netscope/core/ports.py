"""
netscope.core.ports

Port (contract) definitions for the boundary between core and adapters.

WHY THIS CONTRACT EXISTS
-------------------------
core must not depend on infrastructure (docs/architecture/architecture-overview.md
SS2-3, adr-001-architecture-style.md). Concrete measurement logic -- calling
icmplib, dnspython, httpx, opening sockets, shelling out to traceroute/tracert --
belongs in netscope.adapters, never in netscope.core. But core (and, later,
app's orchestration) still needs *something* to depend on in order to request a
measurement and receive a result, without knowing which library or OS facility
actually produced it.

A Protocol is that something: it describes WHAT a probe does (accepts a target,
returns a RawMeasurement) without describing HOW (which library, which platform
branch, which exception types get caught). This is what makes it possible to
add a new probe (TCP, TLS, traceroute) or swap a library (icmplib for something
else) by touching only netscope.adapters -- core and app's use cases never
change, per the "Can a new probe be added without changing the domain?" /
"Can the measurement engine use different third-party libraries later?"
questions in architecture-overview.md SS16 (both answered "yes" on the strength
of this exact boundary).

WHY CONCRETE NETWORK IMPLEMENTATIONS DO NOT BELONG HERE
---------------------------------------------------------
If core imported icmplib/dnspython/httpx/psutil directly, every consumer of
core (including this file itself, and anything that imports it, including
future diagnosis/scoring logic) would transitively depend on every network
library NetScope uses, whether or not that particular consumer cares about
ICMP versus HTTP versus DNS. It would also make core untestable without those
libraries installed and reachable, defeating the entire point of the
pure/offline test strategy already demonstrated by the 51 characterization
tests in tests/test_baseline.py, test_experience_score.py, and
test_diagnosis.py. This module itself imports nothing beyond the Python
standard library and netscope.core.models, and must continue to do so.

HOW FUTURE ADAPTERS ARE EXPECTED TO IMPLEMENT THIS
-----------------------------------------------------
A concrete adapter (e.g. netscope.adapters.probes.icmp.ICMPEchoProbe) is a
plain class with a `probe_type: ProbeType` attribute and a
`run(self, target: str, **options) -> RawMeasurement` method. Because Probe is
a typing.Protocol (structural typing, "duck typing" checked by the type
checker/at runtime via @runtime_checkable), an adapter satisfies this contract
simply by having the right shape -- it does not need to import this module or
subclass anything from it. netscope.app is the only place expected to import
both a concrete adapter and this Protocol together, to wire one to the other
(adr-002-probe-adapter-strategy.md).

REUSE, NOT REINVENTION
------------------------
This module defines a *contract*, not an implementation -- there is nothing
here to reinvent. The actual measurement capability behind each future
adapter (icmplib for ICMP/traceroute, dnspython for DNS, httpx for HTTP,
stdlib socket/ssl for TCP/TLS) is mature, already-evaluated, already-licensed
open-source work documented in docs/architecture/dependency-strategy.md and
adr-002/adr-003; adapters are expected to wrap those libraries directly rather
than reimplementing ICMP/DNS/HTTP/traceroute from scratch. This file exists
solely so that wrapping can happen behind a stable, core-owned shape.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from netscope.core.baseline import UserBaseline
from netscope.core.geo import GeoInfo
from netscope.core.models import ProbeType, RawMeasurement, RouteSnapshot, Service


@runtime_checkable
class Probe(Protocol):
    """Contract every probe adapter (ICMP, DNS, TCP, TLS, HTTP, traceroute)
    must satisfy.

    - What kind of probe it is: the `probe_type` attribute, one of
      ProbeType, so callers (and future diagnosis/scoring logic) can reason
      about which measurement a given result came from without inspecting
      the concrete adapter class.
    - What target it operates on: the `target` parameter of `run()` -- a
      plain string address/hostname, deliberately untyped further here since
      what "target" means (an IP, a hostname, a URL) is probe-specific and
      is the adapter's concern, not core's.
    - How execution is requested: calling `run(target, **options)`.
      `**options` is intentionally open-ended (e.g. timeout, sample count,
      resolver IP) rather than a fixed parameter list, because different
      probe types need different options and core must not need to change
      every time an adapter gains a new tunable.
    - What result is returned: a netscope.core.models.RawMeasurement --
      the existing domain model, reused as-is, not duplicated. Adapters
      that produce route data (traceroute) return RawMeasurement too, with
      hop details carried in RawMeasurement.extra, exactly as
      RawMeasurement is already documented to support in core/models.py;
      this contract does not introduce a second result type for that case.
    """

    probe_type: ProbeType

    def run(self, target: str, **options: Any) -> RawMeasurement: ...


@runtime_checkable
class GeoLookup(Protocol):
    """Contract for offline geographic lookup of one IP address
    (v0.12, TASK-023 "Distance estimation").

    WHY THIS CONTRACT EXISTS
    -------------------------
    `RouteHop.country` has documented itself as a lookup-ready
    placeholder since TASK-020: the traceroute probe must not do
    GeoIP itself (its adapter wraps icmplib, which has no concept of
    geography), and enrichment is display data applied by `app`
    afterwards -- the exact pattern `_enrich_snapshot` already runs
    for ASN and hostnames. This port gives that pattern its third
    lookup, and gives the measurement-round enrichment its own, so
    neither ever depends on a concrete mmdb reader. Like `Probe`,
    this is a capability port, not a repository: one method, pure
    query, no writes.

    CONTRACT SEMANTICS
    --------------------
    - `lookup(ip_address)` returns a `GeoInfo` when the loaded
      database knows the address, or **None when it doesn't** --
      unknown addresses (private space, not-yet-allocated blocks, an
      IPv6 address against an IPv4-only database) are a normal
      result, never an error, and callers must treat the absence of
      geography the way `AsnLookupResult.matched == False` is
      treated: render without it.
    - A syntactically invalid address raises ValueError -- a caller
      programming error, distinct from a legitimate-but-unknown
      address (the same split `AsnLookupAdapter.lookup` draws).
    - The port says nothing about HOW the database is read (which
      file format, which library, whether the user's home is
      configured by coordinates or by IP): that is the adapter's
      concern. It also says nothing about staleness policy -- the
      adapter reports `db_age_days` as data; deciding what "too old"
      means is `core.geo`'s `DB_STALE_AFTER_DAYS`, so the policy stays
      pure and testable without any database at all.

    The reference adapter is `adapters/geoip.py`
    (`MaxMindGeoLookup`, a dependency-free MaxMind DB reader --
    see adr-011-geoip-strategy.md); tests use in-memory fakes, per
    the same offline-test doctrine every other port here follows.
    """

    def lookup(self, ip_address: str) -> GeoInfo | None: ...


@runtime_checkable
class BaselineRepository(Protocol):
    """Contract for persisting/loading the learned personal baseline.

    WHY THIS CONTRACT EXISTS
    -------------------------
    `core.baseline`'s `UserBaseline` is explicitly documented (see
    docs/architecture/module-boundaries.md's Intelligence section) as a
    plain in-memory object that must not persist itself -- baseline
    *state* is persisted by `persistence`, loaded/saved by `app`, via
    this port. Without it, `app` would need to depend on a concrete
    storage implementation (e.g. sqlite3) directly to round-trip a
    `UserBaseline` across runs, which would violate the same
    core-must-not-depend-on-infrastructure rule the `Probe` Protocol
    above exists to uphold (adr-001-architecture-style.md, ADR-005).

    SCOPE OF THIS PORT (TASK-024)
    -------------------------------
    This is the smallest contract that lets `app` round-trip a single
    user's baseline: `save` to persist the current in-memory state, and
    `load` to retrieve it (e.g. on startup, before observing new
    measurements). NetScope is a single-user, local-first tool (ADR-005,
    architecture-overview.md SS14) -- there is exactly one `UserBaseline`
    per local installation, so no per-user key/id parameter is part of
    this contract; whichever single baseline exists locally is what is
    saved and loaded. `load` returning a *fresh* baseline when nothing
    has been saved yet is a concrete `persistence` implementation detail,
    not a concern of this port.

    Per ADR-005, this is one of five aggregate-specific repository ports
    expected in `core.ports` (`MeasurementRepository`, `RouteRepository`,
    `BaselineRepository`, `ServiceRepository`, `IncidentRepository`);
    only `BaselineRepository` is in scope for TASK-024. Concrete SQLite
    persistence is explicitly out of scope here and belongs to a later
    `persistence/` task -- this module continues to define contracts
    only, per this file's existing design (see module docstring above).
    """

    def save(self, baseline: UserBaseline) -> None: ...

    def load(self) -> UserBaseline: ...


@runtime_checkable
class ServiceRepository(Protocol):
    """Contract for persisting the user's curated service list.

    WHY THIS CONTRACT EXISTS
    -------------------------
    ADR-005 names `ServiceRepository` as one of the five aggregate
    repository ports, and `core.models.Service` (TASK-032) documents
    itself as "data someone supplied (config or, eventually,
    persistence)" -- this port is the "eventually": the storage half of
    TASK-032, the piece v0.6's services screen explicitly declined to
    fake (it derived its board from the measurement log rather than
    invent hidden persistence). Without it, `app` would need to depend
    on a concrete storage implementation (e.g. sqlite3) directly to
    remember a user's services across runs, which would violate the same
    core-must-not-depend-on-infrastructure rule the `Probe` and
    `BaselineRepository` contracts above uphold (adr-001-architecture-
    style.md, ADR-005).

    CONTRACT SEMANTICS
    --------------------
    - `save(service)` is an **upsert keyed by `service.name`**: saving a
      Service whose name already exists replaces that entry's host and
      checks in place -- a curated list is edited by re-saving, never
      duplicated under two names.
    - `load_all()` returns every saved Service, **alphabetical by
      name** -- a curated list is short and read often, and a
      deterministic order lets the CLI's `service list` and the TUI's
      services board render the same list the same way. An empty list
      (nothing saved yet) is a normal result, not an error.
    - `remove(name)` deletes the named Service and returns **whether a
      row actually existed** -- the caller can say "removed" versus
      "no such service" honestly, without a separate existence probe
      first.

    Like `BaselineRepository`, this is a single-user, local-first
    contract (ADR-005, architecture-overview.md SS14): exactly one
    curated list per local installation, so no per-user key and no
    sync/remote semantics are part of it.

    Per ADR-005 this is the second of the five expected repository
    ports to land in `core.ports` (after TASK-024's
    `BaselineRepository`); concrete SQLite persistence is explicitly
    out of scope here and belongs to `persistence/` -- this module
    continues to define contracts only, per its existing design.
    """

    def save(self, service: Service) -> None: ...

    def load_all(self) -> list[Service]: ...

    def remove(self, name: str) -> bool: ...


@runtime_checkable
class RouteRepository(Protocol):
    """Contract for persisting route snapshots and loading a target's
    route history.

    WHY THIS CONTRACT EXISTS
    -------------------------
    ADR-005 names `RouteRepository` as one of the five aggregate
    repository ports, and the `route_snapshots`/`route_hops` tables it
    specified have existed in the schema since it was first drawn
    (schema v1) -- but until v0.8, nothing wrote to them: every
    `netscope route` run computed its churn verdict over the single
    in-memory snapshot it had just produced, then threw the path away.
    This port is what gives the route its memory -- `core.routing`'s
    `analyze_route_churn` was always built to compare a SEQUENCE of
    snapshots; storage is what makes such a sequence exist across runs.
    Without it, `app` would need to depend on sqlite3 directly to
    remember a path across runs, violating the same
    core-must-not-depend-on-infrastructure rule the Probe,
    BaselineRepository, and ServiceRepository contracts above uphold
    (adr-001-architecture-style.md, ADR-005).

    CONTRACT SEMANTICS
    --------------------
    - `save(snapshot)` **appends** one observation: routes are a log,
      not a curated list -- every traceroute run is a new data point,
      never an edit of a previous one. The snapshot's hops are stored
      exactly as enriched by the caller; `RouteSnapshot.signature()` is
      address-derived, so lookup enrichment (ASN/hostname) can never
      alter what the history compares.
    - `load_recent(target, limit)` returns the target's most recent
      `limit` snapshots **oldest first** -- chronological order, the
      exact sequence `analyze_route_churn` requires (it compares
      consecutive pairs and explicitly does not sort). Fewer than
      `limit` snapshots (or none at all -- a target never traced before)
      is a normal result, not an error.

    Like the other repository ports, this is a single-user, local-first
    contract (ADR-005, architecture-overview.md SS14): the history a
    local installation observed is what it compares against.

    Per ADR-005 this is the third of the five expected repository ports
    to land in `core.ports` (after TASK-024's `BaselineRepository` and
    v0.7's `ServiceRepository`); concrete SQLite persistence is
    explicitly out of scope here and belongs to `persistence/` -- this
    module continues to define contracts only, per its existing design.
    """

    def save(self, snapshot: RouteSnapshot) -> None: ...

    def load_recent(self, target: str, limit: int = 20) -> list[RouteSnapshot]: ...
