# ADR-010 — Network Interface Discovery

**Status:** Accepted
**Builds on:** `adr-002-probe-adapter-strategy.md` (the same port/adapter pattern,
applied to discovery instead of probing), `adr-007-core-ports-contract.md`,
`adr-009-probe-registry.md`. Resolves the "unused dependency" finding for `psutil`
first raised in `implementation-audit.md` and tracked as a decision in
`dependency-strategy.md`.

## Why discovery belongs outside core

Enumerating network interfaces requires talking to the OS — via `psutil` here,
in principle via platform-specific APIs in other implementations. That is
infrastructure, for exactly the same reason ICMP/DNS/HTTP probing is: it does
real I/O against something outside the process, its results vary by machine
and by moment, and it needs a library (or OS call) `core` must not depend on
to stay offline-testable. `core/discovery.py`'s `DiscoveryProvider` Protocol
is the counterpart of `core/ports.py`'s `Probe` Protocol — it describes *what*
discovery returns (a `NetworkSnapshot`) without describing *how* (`psutil`,
or anything else). `core/discovery.py` imports nothing beyond `typing` and
`netscope.core.models`, enforced by
`tests/test_network_discovery.py::test_core_discovery_module_only_imports_stdlib_typing_and_core_models`,
mirroring the equivalent regression test already in place for `core/ports.py`.

## Adapter responsibility

`adapters/discovery/network_discovery.py`'s `PsutilNetworkDiscovery` is a thin
translation layer, not new measurement logic: it calls
`psutil.net_if_addrs()`/`psutil.net_if_stats()` and maps the result onto
`NetworkInterface`/`NetworkSnapshot` — the two domain models this task adds to
`core/models.py`. It filters out link-layer (`psutil.AF_LINK`, i.e. MAC)
addresses, since `NetworkInterface.addresses` represents IP addresses, and
treats an interface missing from `net_if_stats()` as down rather than raising
— `psutil`'s own documentation doesn't guarantee the two dicts it returns
always share identical keys, and a defensive default is safer than an
unhandled `KeyError` reaching the caller. When `psutil` itself isn't
installed, `discover()` returns a well-formed, empty `NetworkSnapshot` rather
than letting an `ImportError` propagate — the same pattern already used by
`icmp_probe.py`/`dns_probe.py`/`http_probe.py` for their own libraries.

## `psutil` usage decision

`psutil` was flagged by `implementation-audit.md` as declared in
`pyproject.toml` but imported nowhere in `src/`, and `dependency-strategy.md`
conditionally kept it on the strength of an assigned-but-unbuilt purpose in
`adapters/discovery.py` (TASK-010/TASK-011). This task builds that purpose:
`psutil` is now genuinely imported and used, resolving the audit finding
rather than leaving it open indefinitely. No alternative was seriously
considered — `dependency-strategy.md` already evaluated `psutil` against
hand-rolled per-OS interface parsing and found it clearly preferable
(cross-platform, actively maintained, BSD-3-Clause), and this task does not
revisit that evaluation, only acts on it.

**Naming note:** `module-boundaries.md` used a placeholder name,
`NetworkContext`, for a not-yet-designed "assembled discovery result"
concept. This task introduces `NetworkSnapshot` instead, to match the
existing `RouteSnapshot` naming convention in `core/models.py` (both
represent "the state of something at one point in time, subject to change
between measurement rounds") — a naming refinement, not a scope change; the
placeholder name was never implemented as code, so there is nothing to
migrate away from.

## Scope explicitly not covered by this task

Per the task's boundaries and `future-roadmap.md`'s own task split,
`NetworkInterface` intentionally has no gateway, DNS-server, or
connection-type (Wi-Fi/Ethernet/cellular) fields yet — those belong to
TASK-011 (Gateway discovery) and TASK-012 (Network type detection)
respectively, and adding speculative fields now would be exactly the kind of
premature modeling `architecture-overview.md` warns against. No existing
probe, adapter, or test was modified beyond `Container` gaining a new,
backward-compatible field.

## Test strategy

`module-boundaries.md` anticipated this split directly: "unit tests for any
pure post-processing logic ... using hand-constructed `psutil`-shaped fake
data" plus "a small number of manual/opt-in integration tests." This task's
tests follow exactly that shape — every value-asserting test monkeypatches
`psutil.net_if_addrs`/`net_if_stats` with hand-built fake objects (so results
are deterministic regardless of the machine running them), and exactly one
test (`test_container_discovery_provider_snapshot_has_expected_shape_real_adapter_smoke_test`)
calls the real, non-mocked adapter — asserting only structural correctness
(right types, no exception), never specific interface names/addresses, since
those vary by machine. `psutil.net_if_addrs()`/`net_if_stats()` are local
system introspection, not network I/O, so even that smoke test needs no
internet access or real network connection, consistent with the task's
instruction to avoid tests requiring the real machine network.
