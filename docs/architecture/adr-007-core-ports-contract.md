# ADR-007 — Core Ports Contract (`core.ports.Probe`)

**Status:** Accepted
**Implements:** the `Probe` port described conceptually in `adr-002-probe-adapter-strategy.md`
and `architecture-overview.md` §8, as actual code, for the first time (TASK-006).

## Why this contract exists

`core` must not depend on infrastructure (`architecture-overview.md` §2-3,
`adr-001-architecture-style.md`). Concrete measurement logic — `icmplib`,
`dnspython`, `httpx`, sockets, `traceroute`/`tracert` subprocesses — belongs in
`adapters`, never in `core`. But `core` (and later `app`'s orchestration) still
needs something to depend on to request a measurement and get a result back,
without knowing which library or OS facility produced it. `core/ports.py`'s
`Probe` is that something: a `typing.Protocol` describing *what* a probe does
(accepts a target, returns a `RawMeasurement`) without describing *how*.

## Why concrete network implementations do not belong in `core`

If `core` imported `icmplib`/`dnspython`/`httpx`/`psutil` directly, every
consumer of `core` would transitively depend on every network library
NetScope uses, and `core` would stop being testable offline — defeating the
exact property the 51 pre-existing characterization tests already demonstrate
is valuable. `core/ports.py` itself imports nothing beyond `typing` and
`netscope.core.models`, enforced by an AST-based regression test
(`tests/test_ports.py::test_ports_module_only_imports_stdlib_typing_and_core_models`)
rather than left as a one-time manual check.

## How future adapters are expected to implement it

A concrete adapter (e.g. a future `adapters/probes/icmp.py::ICMPEchoProbe`) is
a plain class with a `probe_type: ProbeType` attribute and a
`run(self, target: str, **options) -> RawMeasurement` method. Because `Probe`
is a structural `Protocol`, an adapter satisfies the contract by having the
right shape — no inheritance from `Probe` is required, demonstrated in
`tests/test_ports.py` by fakes that satisfy it without subclassing anything.
`app` is the only package expected to import both a concrete adapter and this
Protocol together, to wire one to the other.

## Reuse over reinvention

No new result/classification model was introduced: `Probe.run()` returns the
already-existing `netscope.core.models.RawMeasurement`, and `probe_type` is
the already-existing `ProbeType` enum — both reused as-is, per the task's
explicit instruction not to duplicate existing domain models. The actual
measurement capability behind each future adapter remains mature, already
license-evaluated open-source work (`dependency-strategy.md`, ADR-002,
ADR-003) that adapters wrap rather than reimplement; this ADR concerns only
the shape of the boundary, not any new implementation.

## Scope explicitly not covered by this decision

Per the task's scope control, no persistence port, clock/time provider, or
measurement-runner/application-service abstraction was added — none is
required yet by any currently-implemented code, and speculative interfaces
for features not yet being built were explicitly avoided. A
`BaselineRepository`-style persistence port remains a `core/ports.py` addition
for whichever future task (`future-roadmap.md` TASK-024 onward) actually needs
it, not this one.
