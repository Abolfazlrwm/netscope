# ADR-002 — Probe Adapter Strategy

**Status:** Accepted

## Context

`core` must never know whether a measurement came from `icmplib`, `dnspython`,
`httpx`, a stdlib `socket`/`ssl` call, or an OS command. The existing MVP
probes (`icmp_probe.py`, `dns_probe.py`, `http_probe.py`, audited in TASK-002)
already follow this pattern informally — each is a thin function that calls
one library and returns a `RawMeasurement`. This ADR formalizes that pattern
as the architecture's `Probe` abstraction and extends it to TCP/TLS/traceroute.

## Decision

### The interface

```python
# core/ports.py (conceptual)
class Probe(Protocol):
    def run(self, target: str, **kwargs) -> Measurement: ...
```

Defined in `core`, implemented in `adapters`. `app` is the only place that
imports both sides.

### Applying the decision hierarchy from the task

| Capability | Hierarchy tier | Decision |
|---|---|---|
| ICMP / ping / packet loss | 1 — mature library exists and fits | `icmplib` (already a dependency, LGPL-3.0, cross-platform) |
| DNS | 1 | `dnspython` (already a dependency, ISC) |
| HTTP | 1 | `httpx` (already a dependency, BSD-3-Clause) |
| TCP | 1 — stdlib is itself the mature, maintained implementation | `socket` (stdlib) — no third-party library needed; a raw TCP connect-timing probe is a handful of lines against `socket.create_connection` |
| TLS | 1 — same reasoning | `ssl` (stdlib), layered on the TCP adapter's socket |
| Traceroute | 1 — `icmplib` already ships one | `icmplib.traceroute()` — see ADR-003 for the full comparison |
| Network interface / gateway discovery | 1 | `psutil` (already a declared dependency; currently unused — this ADR is the use case that justifies keeping it, addressing the audit's "dependency leakage" finding) |
| ASN / GeoIP | 1, with a caveat | `pyasn`/`geoip2` — code is permissively licensed, but GeoLite2 *data* has a separate MaxMind EULA (documented in Phase 1 research §4 and re-confirmed here, not re-litigated) |

No capability in this list reached tier 3 or 4 (implement ourselves) — every
raw measurement capability NetScope needs already has a mature, appropriately
licensed library. This matches Phase 1 research's own conclusion; nothing here
overturns it.

### Where NetScope-specific behavior lives (tier 2 of the hierarchy)

Even where a library is used directly, it only ever produces *raw* numbers —
tier 2's "wrap it behind a NetScope adapter" applies uniformly:

- `icmplib.ping()` returns a `Host`; the adapter converts `packet_loss` (a
  0–1 fraction) to `packet_loss_pct` (0–100), matching the unit convention
  already established and verified correct in TASK-002.
- `icmplib.traceroute()` returns `list[Hop]`; the adapter converts each `Hop`
  (`address`, `avg_rtt`, `packet_loss`, `distance`) into `core.models.Hop`,
  which additionally has `hostname`/`asn`/`organization`/`country` fields that
  `icmplib` does not populate — those come from separate adapter calls
  (reverse DNS via `dnspython`, ASN/GeoIP via their own adapters), composed by
  `app`, not smuggled into the traceroute adapter itself.
- No adapter is ever allowed to attach a diagnosis-shaped judgment ("ISP
  problem") to what it returns — enforced simply by `Measurement`/`Hop` having
  no field for it. A probe reports; `core.diagnosis` interprets. This is the
  layering rule from the task's §3, restated here as an adapter-level
  constraint rather than a general principle.

### Error handling

Each adapter owns a private exception → `ProbeErrorType` mapping (see
architecture-overview.md §6). This mapping is the only adapter-internal code
that needs to know a specific library's exception hierarchy.

### Platform branching

Confirmed by inspecting the currently-installed dependencies: `icmplib`,
`dnspython`, and `httpx` need zero OS branching — `icmp.py`, `dns.py`, and
`http.py` adapters are platform-agnostic. Only `traceroute.py` (privilege
model, see ADR-003) and `discovery.py` (gateway/interface interpretation) need
`if platform.system() == ...` branches, and those branches live inside those
two files. No `platform/` directory tree is created for this (see
architecture-overview.md §7 for the full reasoning).

## Consequences

- Every adapter file maps 1:1 to "one library or stdlib facility, wrapped."
- Swapping `icmplib` for a different ICMP library later touches exactly one
  file (`adapters/probes/icmp.py`) and nothing in `core`.
- TCP/TLS need no new third-party dependency at all — reduces the dependency
  surface relative to what Phase 1 research's dependency table might have
  implied was needed.
