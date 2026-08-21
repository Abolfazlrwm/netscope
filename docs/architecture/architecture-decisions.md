# NetScope — Architecture Decisions

**Status:** Proposed (documentation only)
**Companion to:** `architecture-overview.md`, and the individual ADRs (`adr-00X-*.md`)
which cover architecture style, probe adapters, traceroute, UI framework, persistence,
and dependency policy in full depth. This document covers the decisions those ADRs
don't already own — plus a short summary/cross-reference for the ones they do, so this
file is a complete checklist without duplicating full reasoning.

---

## Python version

**Context:** `pyproject.toml` already declares `requires-python = ">=3.10"`.

**Decision:** Keep `>=3.10`.

**Reason:** The codebase already uses `from __future__ import annotations` plus
built-in generic syntax (`list[RouteHop]`, `dict[str, MetricBaseline]`) throughout
`core/models.py` and `intelligence/baseline.py`, which needs 3.9+ at minimum; 3.10 is
chosen over 3.9 for `match` statements (not yet used, but available for the future
diagnosis hypothesis-matching logic in `architecture-overview.md` §11) and better
`typing.Protocol` ergonomics needed for `core.ports` (§8 of the overview).

**Alternatives considered:**
- 3.9 — rejected, no `match` statement, slightly worse `Protocol`/generics ergonomics for no compatibility benefit NetScope currently needs.
- 3.12/3.13 as a floor — rejected as premature; would exclude users on still-common distro-default Pythons for no feature NetScope currently uses.

**Trade-offs:** None significant — 3.10 is old enough to be widely available, new enough for the syntax already in use.

---

## Package structure

**Context:** See `adr-001-architecture-style.md` in full.

**Decision:** Five packages — `core`, `adapters`, `persistence`, `app`, `ui` — split by dependency direction, not topic.

**Reason / Alternatives / Trade-offs:** Fully documented in ADR-001; summarized in `architecture-overview.md` §2.

---

## Synchronous vs. asynchronous execution

**Context:** The implementation audit explicitly flagged "production code is synchronous" as an observation. All three current probes (`icmplib.ping`, `dns.resolver.Resolver.resolve`, `httpx.Client.get`) block the calling thread; `ui/cli.py` runs them sequentially. `icmplib`, `dnspython`, and `httpx` all already ship async variants (`async_ping`, `dns.asyncresolver`, `httpx.AsyncClient`).

**Decision:** Keep synchronous execution as the default for the CLI/one-shot use case. Design `core.ports.Probe` so an async variant (`AsyncProbe`, or a single `Protocol` with both a sync `run()` and an async `arun()`) can be added later without changing `core`'s pure logic — but do not switch to async now.

**Reason:** A one-shot CLI invocation running 3–6 probes sequentially costs low single-digit seconds at worst (bounded by each probe's own timeout, already small — 2–5s per the audited probe defaults). That's acceptable for a diagnostic command a human runs and reads. Async only pays for itself once NetScope needs to run *many* probes concurrently (e.g. a continuously-updating Textual dashboard refreshing several targets in parallel, or `TASK-033 Service monitoring` checking several services at once) — at which point it's an `adapters`/`app`-level concern, not something that should force complexity into `core` today.

**Alternatives considered:**
- **Async everywhere, now.** Rejected: adds `asyncio` event-loop management to every layer for a benefit (concurrent probing) the current CLI use case doesn't need yet, violating the "avoid unnecessary over-engineering" principle.
- **Threading instead of async for concurrency.** Not rejected outright — viable for `TASK-033`-style parallel service checks without introducing `asyncio` at all, since `icmplib`/`dnspython`/`httpx` are all thread-safe for independent calls. Left as an open option for whichever future task actually needs concurrent probing to decide, informed by which need arrives first (a concurrent-dashboard UI favors async; a "check 5 services in parallel and wait" CLI command favors a simple thread pool).

**Trade-offs:** Revisiting this decision later means adding an `arun()` method to the `Probe` protocol and one async adapter implementation per probe — not a rewrite, because `core` never called the sync methods directly in the first place (`app` did), per the dependency table in `architecture-overview.md` §3.

---

## SQLite

**Context:** See `adr-005-persistence-strategy.md` in full.

**Decision:** Keep SQLite (stdlib `sqlite3`), moved behind repository ports.

**Reason / Alternatives / Trade-offs:** Fully documented in ADR-005; summarized in `architecture-overview.md` §12.

---

## Probe abstraction

**Context:** See `adr-002-probe-adapter-strategy.md` in full.

**Decision:** A `core.ports.Probe` `Protocol` (`run(target, **kwargs) -> Measurement`), implemented per-probe-type in `adapters/probes/`.

**Reason / Alternatives / Trade-offs:** Fully documented in ADR-002; summarized in `architecture-overview.md` §8.

---

## Dependency strategy

**Context:** See `dependency-strategy.md` (companion document, full capability-by-capability table) and `adr-006-dependency-policy.md`.

**Decision:** Prefer mature, permissively-licensed, actively-maintained libraries; wrap every third-party library behind an `adapters/` module; use the stdlib (`socket`/`ssl`/`sqlite3`) instead of a third-party dependency wherever it's sufficient.

**Reason / Alternatives / Trade-offs:** Fully documented in `dependency-strategy.md` and ADR-006.

---

## UI technology

**Context:** See `adr-004-ui-framework.md` in full.

**Decision:** Textual for the MVP UI (already declared as an optional extra in `pyproject.toml`, unused so far).

**Reason / Alternatives / Trade-offs:** Fully documented in ADR-004.

---

## CLI strategy

**Context:** Today's `ui/cli.py` uses `argparse` directly and mixes argument parsing, orchestration, and `print()`-based presentation in one module (audited in TASK-002, characterized by TASK-003's tests indirectly by testing everything *around* it).

**Decision:** Keep `argparse` for the CLI's argument parsing (no reason to add a third-party CLI framework — `argparse` is stdlib, sufficient for NetScope's flat command surface, and already working). The CLI becomes a thin `ui/cli.py` that parses arguments, calls exactly one `app` use case, and formats the result — no orchestration logic remains in `ui/`.

**Reason:** `argparse`'s feature set (subcommands, `--flag value` parsing, help text) already covers everything the roadmap's CLI tasks need (`TASK-035` foundation, `TASK-036` diagnostic command, `TASK-037` route command, `TASK-038` JSON output, `TASK-039` report export) without a new dependency. The problem the audit found was never *which* CLI library was used — it was that the CLI module did orchestration work that belongs in `app`.

**Alternatives considered:**
- `click`/`typer` — more ergonomic for larger CLIs with many subcommands and rich help formatting, but NetScope's command surface (diagnose, route, history, service) is small enough that `argparse` doesn't create real friction; adding a dependency for marginal ergonomics isn't justified yet. Revisit if the CLI surface grows past what `argparse` handles comfortably.

**Trade-offs:** If NetScope's CLI surface grows substantially (many subcommands, complex flag validation, shell-completion needs), revisiting this in favor of `click`/`typer` is cheap — it only touches `ui/`, per the dependency table in `architecture-overview.md` §3.

---

## Configuration strategy

**Context:** The audit found configuration (target hosts, timeouts, sample counts) scattered as hardcoded defaults across `probes/icmp_probe.py`, `probes/dns_probe.py`, `probes/http_probe.py`, and module-level constants in `ui/cli.py` (`PUBLIC_DNS`, `PUBLIC_CDN_HTTP`).

**Decision:** A single configuration object, loaded once in `app`'s composition root and passed down explicitly (not read from a global/singleton) to whichever adapters/use cases need it. Backed by a plain, versionable file format (TOML, since Python 3.11+ has `tomllib` in the stdlib and the project already uses `pyproject.toml`'s TOML syntax) with built-in defaults so NetScope runs with zero required configuration.

**Reason:** Centralizing configuration removes the audit's "missing boundary" finding directly, and passing it explicitly (rather than a global) keeps `core`/`adapters` testable with fake config objects, consistent with the test-strategy table in `architecture-overview.md` §3.

**Alternatives considered:**
- Environment variables only — rejected as the sole mechanism (fine as an *override* layer later, but awkward for structured per-target/per-service configuration like the `Service` model in §5 of the overview).
- YAML — rejected in favor of TOML to avoid adding a YAML-parsing dependency (`tomllib` is stdlib on 3.11+; on 3.10 the lightweight `tomli` backport would be needed — a decision to make at implementation time, not blocking this architecture decision).

**Trade-offs:** On Python 3.10 specifically, `tomllib` isn't in the stdlib yet (added in 3.11), so the config-loading task will need to either bump the floor to 3.11 or add `tomli` as a small, permissively-licensed (MIT) dependency — flagged here so it isn't a surprise when `TASK-0XX` (configuration, not yet numbered in the current roadmap — see `future-roadmap.md`'s note) is picked up.

---

## Logging

**Context:** No logging strategy exists yet in the codebase or prior research/architecture documents.

**Decision:** Use the Python stdlib `logging` module, one logger per top-level package (`logging.getLogger("netscope.adapters")`, etc.), configured once by `app`'s composition root (never by `core`, which shouldn't have side effects at import time). Default to `WARNING` level with a `--verbose`/`-v` CLI flag to raise it, consistent with typical CLI tool conventions.

**Reason:** No new dependency needed; `logging`'s hierarchical logger names map naturally onto the five-package structure. Keeping configuration in `app` (not `core`) matches the dependency-direction rule in `architecture-overview.md` §3 — `core` must stay side-effect-free and safely importable in tests without needing logging setup first.

**Alternatives considered:**
- `structlog` or another structured-logging library — not rejected outright, just not justified yet; stdlib `logging` is sufficient for a single-process CLI tool, and adding a dependency for structured log output is premature before there's a consumer (e.g. a log-aggregation pipeline) that needs it.

**Trade-offs:** Per the privacy principles (§14 of the overview / §"Security & Privacy" below), log output must not include full DNS answer payloads or other target-identifying detail at default (`WARNING`) verbosity — this needs to be an explicit review item on whichever future task adds the first real log statements, not an afterthought.

---

## Error handling

**Context:** See `architecture-overview.md` §6 ("Error model") for the full structured-error design (`ProbeErrorType` enum, adapter-owned exception mapping). This entry exists to record it as a decision, not to duplicate the design.

**Decision:** Structured errors (`ProbeErrorType` + message) attached to `Measurement`, replacing today's bare `error: Optional[str]`. Each adapter owns a private exception→`ProbeErrorType` mapping; `core` only ever branches on the typed enum, never on exception classes or string content.

**Reason / Alternatives / Trade-offs:** Fully documented in `architecture-overview.md` §6.

---

## Cross-platform strategy

**Context:** See `adr-002-probe-adapter-strategy.md` §"Platform branching" and `architecture-overview.md` §7.

**Decision:** No dedicated `platform/windows|linux|macos/` tree. OS branching (only needed for traceroute privilege handling and gateway/interface discovery) lives inside the one or two `adapters/` files that need it.

**Reason / Alternatives / Trade-offs:** Fully documented in `architecture-overview.md` §7 and ADR-002.

---

## Privacy model

**Context:** See `architecture-overview.md` §14 ("Security & privacy") and the implementation audit's STEP 8 privacy audit (found no violations in the current MVP).

**Decision:** Local-first by default and by construction: `persistence` defaults to a local SQLite file; no component other than `adapters/probes/*` is permitted to open a network connection, and only to a `Measurement`/`RouteSnapshot`'s declared target; any future telemetry must be opt-in, off by default, and isolated to `app`'s composition root as an optional injected sink.

**Reason / Alternatives / Trade-offs:** Fully documented in `architecture-overview.md` §14.
