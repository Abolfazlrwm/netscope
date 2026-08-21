# NetScope — Dependency Strategy

**Status:** Proposed (documentation only)
**Companion to:** `adr-002-probe-adapter-strategy.md`, `adr-003-traceroute-strategy.md`,
`adr-004-ui-framework.md`, `adr-005-persistence-strategy.md`, `adr-006-dependency-policy.md`.
Those ADRs record the *decisions*; this document is the fuller candidate-by-candidate
evaluation table the task asked for, so every capability's alternatives are visible in
one place even where the ADRs above already settled the question.

The fact that a library exists does not automatically mean NetScope should use it —
every row below was evaluated against: does NetScope actually need this capability
yet, does a mature option already exist, and is a new dependency actually cheaper than
using the standard library.

---

## Capability-by-capability table

| Capability | Candidate | License | Maintenance | Cross-platform | Recommendation | Reason |
|---|---|---|---|---|---|---|
| ICMP | `icmplib` (installed: 3.0.4) | LGPL-3.0-or-later | Active | Yes (Windows/Linux/macOS) | **Use — already a dependency** | Most accurate/safe ICMP library for Python; unprivileged-socket fallback already implemented and verified in the audited MVP |
| ICMP (alt.) | `ping3` | MIT | Low activity | Yes | **Do not add** | Simpler API but less accurate, no traceroute, no unprivileged mode — `icmplib` already covers everything it would |
| DNS | `dnspython` (installed: 2.8.0) | ISC | Active | Yes | **Use — already a dependency** | De-facto standard; already verified against the installed 2.8.0 API in the implementation audit |
| DNS (alt.) | stdlib `socket.gethostbyname` | PSF (stdlib) | N/A | Yes | **Do not use as primary** | No custom resolver support, no record-type control, no timing precision needed for a DNS *probe* specifically |
| HTTP | `httpx` (installed: 0.28.1) | BSD-3-Clause | Active | Yes | **Use — already a dependency** | HTTP/2 support, async-capable for future concurrent probing, already verified working |
| HTTP (alt.) | `requests` | Apache-2.0 | Active | Yes | **Do not add** | Sync-only, no HTTP/2 — no advantage over `httpx` for this project |
| TCP | stdlib `socket` | PSF (stdlib) | N/A | Yes | **Use, no new dependency** | Connect-timing is a few lines against `socket.create_connection`; no third-party library adds meaningful value |
| TLS | stdlib `ssl` | PSF (stdlib) | N/A | Yes | **Use, no new dependency** | Handshake timing/cert inspection via stdlib `ssl`, layered on the TCP adapter's socket |
| TLS (alt.) | `cryptography` | Apache-2.0 / BSD | Active | Yes | **Not needed yet** | Only relevant if NetScope needs deep certificate parsing beyond what stdlib `ssl` exposes (e.g. full X.509 field extraction) — not required for handshake-timing probing; revisit if `TASK-017 TLS` needs it |
| Traceroute | `icmplib.traceroute()` | LGPL-3.0-or-later | Active (same package as ICMP) | Yes, but requires root/Administrator unconditionally (verified from the installed package's own docstring) | **Use — already a dependency, verified present** | See `adr-003-traceroute-strategy.md` in full; no new dependency needed, typed `Hop` output, confirms rather than contradicts Phase 1 research |
| Traceroute (fallback) | system `traceroute`/`tracert` via `subprocess` | N/A (invokes OS binary, not linked) | N/A | Needs three separate output parsers (Linux/macOS/Windows) | **Document as optional fallback only** | For environments where raw ICMP sockets are unavailable to the process at all; not the primary path |
| Traceroute (rejected) | `scapy` | GPL-2.0 | Active | Yes (needs Npcap on Windows) | **Do not add** | Already rejected in Phase 1 research and `NOTICE`; `icmplib.traceroute()` makes this moot anyway |
| ASN lookup | `pyasn` | BSD-2-Clause | Low activity but stable/functional | Yes | **Not yet added — approved for future use** | Offline ASN lookup against a periodically-updated local database; no live third-party API call needed, consistent with local-first principle |
| ASN lookup (alt.) | `ipwhois` | BSD | Active | Yes | **Not yet added — secondary option** | Simpler API but relies on live WHOIS/RDAP queries by default, which is a network dependency beyond the target itself — only appropriate if offline `pyasn` data proves insufficient |
| Geo/IP | `geoip2` (code) + MaxMind GeoLite2 (data) | Code: Apache-2.0. **Data: separate MaxMind GeoLite2 EULA — not a standard OSS license**, requires free account registration | Active | Yes | **Not yet added — approved for future use, with an explicit licensing caveat** | Only mature offline option; the data license must be satisfied as its own reviewable task (attribution string already pre-drafted in `NOTICE`), not folded into an unrelated change |
| Geo/IP (rejected for now) | Live third-party geolocation HTTP APIs | Varies | Varies | Yes | **Do not add** | Violates the local-first/offline principle (`architecture-overview.md` §14) — would require a network call to a third party just to interpret a traceroute hop |
| Network interfaces | `psutil` (installed: 7.2.2, declared `>=6.0.0`) | BSD-3-Clause | Active | Yes | **Keep, now with an assigned purpose** — resolves the audit's "unused dependency" finding | Cross-platform interface/gateway enumeration without hand-rolled OS-specific parsing; purpose assigned to `adapters/discovery.py` (`TASK-010`/`TASK-011`) |
| SQLite | stdlib `sqlite3` | PSF (stdlib) | N/A | Yes | **Use, no new dependency** | No server process, ships with Python, sufficient for single-user local-first storage; see `adr-005-persistence-strategy.md` |
| SQLite (ORM option) | `SQLAlchemy` or `sqlmodel` | MIT | Active | Yes | **Not decided — implementation detail, deferred** | An ORM is optional and invisible to `core`/`app` behind repository ports (per ADR-005); whoever implements `persistence` can choose raw `sqlite3` (as today) or an ORM without this strategy document needing to change |
| Charts | none currently in scope | — | — | — | **Do not add yet** | No current task in the roadmap renders a chart; the CLI (`TASK-035`–`TASK-039`) is text/JSON output, and the UI phase (`TASK-040`–`TASK-046`) is Textual-based (terminal), which has its own text/sparkline-style rendering primitives that don't need a separate charting library. Revisit only if a graphical (non-terminal) UI is added later. |
| Desktop UI | `textual` (declared as optional extra, not yet installed) | MIT | Active | Yes | **Keep as optional extra — see `adr-004-ui-framework.md`** | Same-language UI, good fit for a diagnostics dashboard; correctly scoped as optional so it isn't installed for CLI-only use |
| Desktop UI (alt.) | `PySide6` | LGPL-3.0 (free/dynamic use; commercial license also available) | Active | Yes | **Not chosen for MVP, remains a valid future option** | Heavier packaging, adds Qt/C++-adjacent concepts; only justified if NetScope needs native graphics later |
| Desktop UI (alt.) | `Tauri` | MIT + Apache-2.0 | Active | Yes | **Not chosen for MVP, remains a valid future option** | Adds a second language (Rust) and a second dependency ecosystem (npm) to an otherwise pure-Python project |
| CLI framework | stdlib `argparse` | PSF (stdlib) | N/A | Yes | **Use, no new dependency** | See `architecture-decisions.md` "CLI strategy" — sufficient for NetScope's current command surface |
| CLI framework (alt.) | `click` / `typer` | BSD-3-Clause / MIT | Active | Yes | **Not yet justified** | More ergonomic for large multi-command CLIs; revisit only if `argparse` becomes a real limitation |
| Config parsing | stdlib `tomllib` (3.11+) or `tomli` (3.10 backport, MIT) | PSF (stdlib) / MIT | N/A / Active | Yes | **Approved for future use — see caveat in `architecture-decisions.md`** | TOML matches the project's existing `pyproject.toml` syntax; `tomllib` needs Python 3.11+, so either the floor bumps to 3.11 or `tomli` is added as a small dependency when the configuration task is implemented |
| Logging | stdlib `logging` | PSF (stdlib) | N/A | Yes | **Use, no new dependency** | See `architecture-decisions.md` "Logging" |
| Test runner | `pytest` (dev-only, installed: 9.1.1) | MIT | Active | Yes | **Keep — already in use for 51 tests** | Already proven with TASK-003's characterization tests |

---

## Dependencies explicitly identified as NOT to introduce yet

| Dependency | Why not yet |
|---|---|
| `scapy` | GPL-2.0; superseded by `icmplib` for both ping and traceroute, and by stdlib `socket`/`ssl` for TCP/TLS |
| `nmap` / `python-nmap` | NPSL license (non-standard, distribution restrictions); no current roadmap task needs port scanning |
| Live third-party GeoIP/geolocation HTTP APIs | Violates local-first principle |
| Any cloud SDK / telemetry backend | No current feature requires one; would also violate the "no required cloud infrastructure" principle |
| `SQLAlchemy`/`sqlmodel` | Not rejected, just not decided — an implementation detail inside `persistence`, deferred until that task is picked up |
| `click`/`typer` | `argparse` is currently sufficient; adding either now would be a dependency without a justifying need |
| `structlog` | stdlib `logging` is sufficient for a single-process CLI tool at this stage |
| `cryptography` | Not needed until TLS probing requires certificate-field-level detail beyond stdlib `ssl` |

## Current dependencies re-evaluated for removal

| Dependency | Currently used? | Recommendation |
|---|---|---|
| `psutil` | **No** — declared in `pyproject.toml`, installed, but not imported anywhere in `src/` today (confirmed in the implementation audit) | **Do not remove — keep, conditionally.** This document assigns it an explicit purpose (`adapters/discovery.py`, `TASK-010`/`TASK-011`, both early in the roadmap). If those tasks are not implemented within the next few roadmap iterations, this recommendation should be revisited and the dependency dropped until it's actually needed — an assigned-but-unbuilt purpose is better than no purpose, but it is not a permanent excuse to keep an unused dependency indefinitely. |
| `icmplib`, `dnspython`, `httpx` | Yes, all three actively imported and used | Keep, no change |
| `textual` (optional extra) | No, not installed by default, no `ui/tui/` code exists yet | Keep as an optional extra — correctly not a hard dependency for CLI-only use |
| `pytest` (dev extra) | Yes, running 51 tests | Keep |

No dependency is added, removed, or version-changed in `pyproject.toml` as part of this
task — the table above is the evaluation; any resulting change to `pyproject.toml`
happens in a future implementation task, not here.
