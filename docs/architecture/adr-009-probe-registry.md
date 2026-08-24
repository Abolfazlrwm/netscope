# ADR-009 — Probe Registry & Composition Root

**Status:** Accepted
**Builds on:** `adr-002-probe-adapter-strategy.md`, `adr-007-core-ports-contract.md`,
`adr-008-probe-adapter-implementation.md`. This ADR connects the adapters those
established to a lookup layer and a minimal composition root (TASK-009).

## Why the registry exists

By the end of TASK-007, three concrete adapters existed
(`ICMPProbeAdapter`, `DNSProbeAdapter`, `HTTPProbeAdapter`), each satisfying
`core.ports.Probe`, but nothing yet let a caller ask for "the probe for this
`ProbeType`" without importing and naming the concrete class directly. The
`ProbeRegistry` (`adapters/probes/registry.py`) is that lookup: a
`ProbeType -> Probe` mapping with a `get()` method, a `register()` method for
extending or overriding it, and an explicit, named exception
(`ProbeNotRegisteredError`, a `LookupError` subclass) for the case where no
adapter exists for the requested type yet (`TCP`/`TLS`/`TRACEROUTE` today).
That last part matters on its own: silently returning `None`, or letting a
bare `KeyError` propagate with no context, would turn a straightforward
"this probe isn't built yet" into a confusing failure several calls away from
its actual cause. `test_unregistered_probe_type_raises_probe_not_registered_error`
and `test_empty_registry_raises_for_every_probe_type` pin this behavior down.

## Why UI should not construct adapters

Per `module-boundaries.md`'s UI section, `ui` must not import `adapters`
directly — doing so would mean every place in the UI that wants to run a
probe needs to know which concrete class implements it, coupling
presentation code to implementation choices that `adr-002` explicitly reserves
for `adapters`/`app` to change freely (e.g. swapping `icmplib` for a different
ICMP library later). The registry turns "which class implements ICMP" into
"ask for `ProbeType.ICMP`" — a `ProbeType` is already a `core` concept `ui`
is allowed to know about; the concrete adapter class is not.

## Composition root responsibility

`app/container.py`'s `Container` (holding a `ProbeRegistry`) and
`build_container()` (constructing the real, production one) exist because
*something* has to actually import a concrete adapter class to put it in the
registry in the first place — that has to happen somewhere, and
`adr-002-probe-adapter-strategy.md` already designated `app` as "the only
place that imports both `core.ports.Probe` and a concrete `adapters.probes.*`
class together." `container.py` is that place, and only that place, for
probes. Consistent with the task's instruction not to build a framework, this
is deliberately not a generic DI container: no auto-wiring, no configuration
file, no reflection-based discovery — `build_container()` is a plain function
that constructs one dataclass. Tests that need different probes (e.g. fakes)
construct their own `ProbeRegistry` and pass it to `Container` directly, which
`test_container_accepts_an_explicit_registry_for_testing` demonstrates, rather
than needing any framework-level override mechanism.

## Reuse of existing probe implementations

Nothing about this task touches `icmp_probe.py`, `dns_probe.py`,
`http_probe.py`, or the TASK-007 adapters wrapping them. The registry's
default mapping (`ProbeRegistry._default_probes()`) simply instantiates the
three already-existing, already-tested adapter classes — it is a lookup table
over work already done, not new measurement logic. This is verified directly
by a regression test (`test_registry_module_does_not_import_network_libraries_directly`)
asserting `registry.py` never imports `icmplib`/`dns`/`httpx`/`psutil`, mirroring
the equivalent guard already in place for `core/ports.py` (TASK-006) and the
adapter modules themselves (TASK-007).

## Consequences

- Adding a new probe (e.g. a future TCP adapter) requires exactly one line of
  wiring — adding it to `_default_probes()` (or calling `register()`) — and
  touches no `core` code, matching the "New probe" row of the extensibility
  table in `architecture-overview.md` §2.
- `app` now has its first real content beyond a placeholder docstring, without
  yet implementing any actual use case (`run_measurement_round()`, etc.) —
  that remains future, separately-scoped work per `future-roadmap.md`.
- `ui/cli.py` is unchanged by this task; it still constructs nothing from
  `adapters` because it doesn't yet call into `app` at all — wiring `ui` to
  `Container`/`ProbeRegistry` is itself a future task (`future-roadmap.md`
  TASK-035, CLI foundation), not this one.
