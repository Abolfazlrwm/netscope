# ADR-006 — Dependency Policy

**Status:** Accepted

## Policy

For every dependency NetScope takes on:

1. Prefer mature, actively-maintained projects over new/experimental ones.
2. Prefer permissive licenses (MIT/BSD/Apache-2.0/ISC) over copyleft
   (LGPL/GPL) or non-standard (NPSL) ones; copyleft is acceptable only when
   used strictly as an unmodified dynamic dependency, never vendored/modified,
   and always disclosed in `NOTICE`.
3. Prefer the standard library over a third-party package when the stdlib
   facility is sufficient (e.g. TCP/TLS probing — see ADR-002).
4. Avoid two libraries that solve the same problem (e.g. do not add `ping3`
   alongside `icmplib` "just in case" — pick one, per Phase 1 research's own
   comparison).
5. Wrap every external library behind an `adapters/` module; never let a
   third-party type appear in a `core` function signature or be stored
   directly in a `core` model.
6. Do not add a dependency ahead of the code that uses it (this is exactly the
   `psutil` finding from the implementation audit — declared but unused,
   flagged there as a hygiene issue, resolved here by giving it an explicit
   purpose, see below).

## Full dependency table

| Dependency | Purpose | Layer | License | Reason | Alternative considered | Decision |
|---|---|---|---|---|---|---|
| `icmplib` | ICMP ping **and** traceroute (ADR-003) | `adapters` | LGPL-3.0-or-later | Most accurate/safe ICMP library for Python; now also covers traceroute, removing the need for a second library | `ping3` (MIT, simpler but less accurate, no traceroute) | Keep, and expand its use to cover traceroute (ADR-003) |
| `dnspython` | DNS resolution, plus reverse-DNS lookups for traceroute hops | `adapters` | ISC | De-facto standard DNS library for Python | stdlib `socket.gethostbyname` (too limited — no custom resolver support, no record-type control) | Keep |
| `httpx` | HTTP probing | `adapters` | BSD-3-Clause | Async-capable, HTTP/2 support, modern API | `requests` (sync-only, no HTTP/2) | Keep |
| `psutil` | Gateway/network-interface discovery (`adapters/discovery.py`) | `adapters` | BSD-3-Clause | Cross-platform system/network info without OS-specific code | Hand-rolled per-OS parsing of `ip route`/`ipconfig`/`route -n` output | **Keep, now with an assigned purpose** — resolves the audit's "declared but unused" finding by specifying exactly which future adapter (`discovery.py`) will use it |
| stdlib `socket`/`ssl` | TCP connect timing, TLS handshake timing | `adapters` | PSF License (stdlib) | No third-party library needed — this is a handful of lines against a well-understood stdlib API | `scapy` (rejected — GPL-2.0, and overkill for simple connect/handshake timing) | Use stdlib, no new dependency |
| `textual` (optional `ui` extra) | Terminal UI | `ui` | MIT | Same-language UI, good fit for a diagnostics dashboard (ADR-004) | PySide6 (LGPL-3.0, heavier), Tauri (adds Rust/npm) | Keep, still optional/not installed by default |
| stdlib `sqlite3` | Persistence | `persistence` | PSF License (stdlib) | No server process, ships with Python, sufficient for single-user local-first storage (ADR-005) | Embedded Postgres (rejected — needs a server process) | Keep |
| `pyasn` or `geoip2` (not yet added) | ASN / GeoIP lookups for `Hop.asn`/`organization`/`country` | `adapters` | `pyasn`: BSD-2-Clause; `geoip2` code: Apache-2.0 (GeoLite2 **data** is under a separate MaxMind EULA, not a standard OSS license) | Only mature, actively-maintained options for offline ASN/GeoIP lookup in Python | Live third-party HTTP APIs (rejected — violates the local-first/offline principle in architecture-overview.md §14) | Not yet added; when it is, the GeoLite2 EULA/attribution requirement (already pre-emptively documented in `NOTICE`) must be satisfied as its own reviewable task, not folded into an unrelated change |
| `pytest` (dev-only) | Test runner | none (dev tooling, never shipped) | MIT | Already in use for TASK-003's 51 tests | `unittest` (stdlib, but pytest's fixtures/assertions are already in use and working) | Keep |

## Explicitly rejected dependencies (carried forward from Phase 1 research and the audit, not re-litigated, only indexed here for completeness)

| Dependency | Reason rejected |
|---|---|
| `scapy` | GPL-2.0; also unnecessary now that `icmplib` covers ping and traceroute, and stdlib `socket`/`ssl` covers TCP/TLS (ADR-002, ADR-003) |
| `nmap` / `python-nmap` | NPSL license (non-standard, distribution restrictions); no current NetScope feature needs port-scanning |
| `ntopng`, `LibreNMS`, `Netdata`, `SmokePing`, `MTR`, `WinMTR` source | All GPL-2.0/3.0 (or GPL-family); none were ever dependencies, only research subjects — re-affirmed here that no source from them is used |

## Consequences

- No new runtime dependency is added by this task (consistent with the task's
  explicit instruction not to add dependencies) — `pyasn`/`geoip2` are named
  and evaluated but not installed.
- The `psutil` hygiene issue from the audit is resolved *as a decision*
  (assign it a purpose) without touching `pyproject.toml` in this task; the
  actual wiring (`adapters/discovery.py`) remains future implementation work.
