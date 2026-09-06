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
from netscope.core.models import ProbeType, RawMeasurement


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

    def run(self, target: str, **options: Any) -> RawMeasurement:
        ...


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

    def save(self, baseline: UserBaseline) -> None:
        ...

    def load(self) -> UserBaseline:
        ...
