# NetScope — Implementation Audit & Architecture Reconciliation

**Scope:** documentation only. No application code, dependencies, or tests
were modified, added, or removed while producing this audit.

---

## STEP 1 — INVENTORY

### Source files

| File | Responsibility |
|---|---|
| `src/netscope/core/models.py` | Domain dataclasses: `RawMeasurement`, `RouteHop`, `RouteSnapshot`, `ExperienceEvent`/`ExperienceLevel`, `Incident`. No behavior beyond a `signature()` helper on `RouteSnapshot` and an `is_active` property on `Incident`. |
| `src/netscope/probes/icmp_probe.py` | Wraps `icmplib.ping()`. Produces a `RawMeasurement` from an ICMP echo sweep. Handles privileged/unprivileged socket fallback. |
| `src/netscope/probes/dns_probe.py` | Wraps `dns.resolver.Resolver.resolve()`. Times a single DNS query, optionally against an explicit resolver IP. Produces a `RawMeasurement`. |
| `src/netscope/probes/http_probe.py` | Wraps `httpx.Client.get()`. Times a single HTTP GET. Produces a `RawMeasurement`. |
| `src/netscope/intelligence/baseline.py` | `MetricBaseline` (Welford's online mean/variance) and `UserBaseline` (per-target latency/loss baselines). Pure, in-memory, no I/O. |
| `src/netscope/intelligence/experience_score.py` | Converts a list of `RawMeasurement` into one `ExperienceEvent` (0–100 score) using fixed latency/loss thresholds. |
| `src/netscope/diagnosis/engine.py` | Rule-based `diagnose()` comparing gateway/public-DNS/public-CDN measurements against fixed thresholds to localize a problem. Defines its own `Diagnosis` dataclass. |
| `src/netscope/explanation/explainer.py` | Formats a `Diagnosis` into human-readable text. No logic beyond string formatting. |
| `src/netscope/persistence/sqlite_store.py` | `SqliteStore`: local SQLite file at `~/.netscope/netscope.db`, one `measurements` table, `save()`/`recent()`/`close()`. |
| `src/netscope/ui/cli.py` | `run_once()` orchestrates probes → score → diagnose → explain → persist, and prints results. `main()` is the `argparse` entry point. |
| `src/netscope/routing/__init__.py` | Empty. Placeholder package only. |
| `src/netscope/monitoring/__init__.py` | Empty. Placeholder package only. |
| `src/netscope/reporting/__init__.py` | Empty. Placeholder package only. |
| `src/netscope/infrastructure/__init__.py` | Empty. Placeholder package only. |
| `src/netscope/intelligence/services/__init__.py` | Empty. Placeholder package only. |

### Test files

None. The `tests/` directory exists but contains zero files. All verification done so far was ad hoc (`python -c "..."` run manually in a previous session), not committed as automated tests.

### Documentation files

| File | Content |
|---|---|
| `README.md` | What's implemented, what isn't, install/run instructions, license summary. |
| `NOTICE` | Third-party license attributions (icmplib, dnspython, httpx, psutil, textual) and an explicit statement that no GPL/NPSL source was copied. |
| `LICENSE` | MIT license text for NetScope's own code. |
| `NetScope-Research-Phase1.md` (delivered separately, not in repo) | The research/architecture document this audit compares against. |
| `docs/architecture/implementation-audit.md` | This file. |

### Dependencies (declared in `pyproject.toml`, verified against what's installed)

| Package | Declared | Installed | Actually imported in `src/`? |
|---|---|---|---|
| `icmplib` | `>=3.0.4` | 3.0.4 | Yes (`probes/icmp_probe.py`) |
| `dnspython` | `>=2.8.0` | 2.8.0 | Yes (`probes/dns_probe.py`, as `dns.resolver`) |
| `httpx` | `>=0.28.1` | 0.28.1 | Yes (`probes/http_probe.py`) |
| `psutil` | `>=6.0.0` | 7.2.2 | **No — not imported anywhere in `src/`** |
| `textual` (optional extra `ui`) | `>=0.60.0` | not installed | No (correctly optional, not yet needed) |
| `pytest` (optional extra `dev`) | `>=8.0.0` | not installed | No test files exist to run it against |

### CLI entry points

- `netscope` console script → `netscope.ui.cli:main` (registered in `pyproject.toml`, confirmed installed via `pip install -e .`).

### Database components

- One SQLite database, one table (`measurements`), created lazily on first `SqliteStore()` instantiation. No migrations mechanism. No tables yet for baselines, incidents, or route snapshots, even though `core/models.py` already defines dataclasses for the latter two.

---

## STEP 2 — ARCHITECTURE AUDIT

### Current architecture (as actually implemented, not as documented)

```text
                    ┌───────────────────────────┐
                    │        ui/cli.py           │
                    │  (argparse + orchestration │
                    │   + printing, all in one)  │
                    └──────────────┬─────────────┘
                                   │ imports directly, no boundary
        ┌──────────────┬──────────┼───────────┬────────────────┐
        ▼              ▼          ▼            ▼                ▼
  probes/icmp_probe  probes/dns_probe  probes/http_probe  intelligence/    persistence/
        │              │          │      experience_score   sqlite_store
        │              │          │            │                 ▲
        └──────────────┴──────────┴──────┬─────┘                 │
                                          ▼                       │
                                  core/models.RawMeasurement ─────┘
                                          │
                                          ▼
                              diagnosis/engine.diagnose()
                             (static thresholds, own Diagnosis
                              model, does NOT consult baseline)
                                          │
                                          ▼
                            explanation/explainer.explain()

  intelligence/baseline.py: fully implemented, mathematically correct,
  but not imported or called from anywhere else in the codebase.
  It is dead code at runtime.
```

### Good decisions

- Each probe module (`icmp_probe`, `dns_probe`, `http_probe`) is a thin, isolated wrapper that converts a third-party library's return type into the shared `RawMeasurement` model. This is exactly the "library gives raw numbers, NetScope decides what they mean" boundary the research doc asked for, at the probe level.
- Each probe guards its import (`try/except ImportError`) and degrades to a `RawMeasurement(success=False, error=...)` instead of crashing — consistent, predictable failure mode.
- `explanation/explainer.py` is correctly decoupled from `diagnosis/engine.py`: it only formats an already-computed `Diagnosis`, never computes anything. This matches the architecture's stated reason for splitting `diagnosis/` and `explanation/` into separate packages.
- `persistence/sqlite_store.py` is genuinely local-first: no network calls, no telemetry, a single file under the user's home directory.
- License hygiene at the dependency-selection level was followed correctly (see STEP 4): no GPL/NPSL package was made a hard dependency.

### Questionable decisions

- `ui/cli.py` directly imports from `probes`, `intelligence`, `diagnosis`, `explanation`, and `persistence` — five different layers — with nothing in between. The architecture document proposed `core/use_cases.py` as the orchestration layer; it was never created, and `cli.py` absorbed that responsibility instead.
- Two independent, static-threshold scoring systems exist side by side: `experience_score.py` (`LATENCY_GOOD_MS=40`, `LATENCY_BAD_MS=250`, `LOSS_BAD_PCT=5.0`) and `diagnosis/engine.py` (`latency_ms > 250`, `packet_loss_pct > 5`). Neither consults `intelligence/baseline.py`. This is the same "fixed global threshold" pattern the research document explicitly identified as what NetScope should **not** do (row 12 of the research comparison table, Zabbix/Icinga/Nagios).
- `diagnosis/engine.py` defines its own `Diagnosis` dataclass (`likely_cause`, `confidence_pct`, `evidence`, `ruled_out`) that duplicates most of `core.models.Incident` (`likely_cause`, `confidence_pct`, `evidence`, `explanation`). Two parallel models for the same concept, unreconciled.
- `diagnosis.engine.diagnose()` treats an *absent* measurement the same as a *healthy* one: `is_bad(None)` returns `False`. In `cli.run_once()`, if no `--gateway` is passed, `local_gateway` is `None`, and the engine silently concludes "gateway is healthy" rather than "gateway was not tested." This produced a misleadingly confident "ruled out: local network issue" in the STEP 1 smoke test even though the local gateway was never probed.

### Unnecessary abstractions

- None found that are actively harmful. The empty `routing/`, `monitoring/`, `reporting/`, `infrastructure/`, `intelligence/services/` packages are placeholders, not abstractions — they cost nothing at runtime and match the research architecture's intended shape. They are not "unnecessary," but they are currently unused, which STEP 10 addresses.

### Missing boundaries

- No application/use-case layer between `ui/cli.py` and the domain packages (see "questionable decisions" above). Adding a second UI (Textual) today would require either duplicating `run_once()`'s orchestration logic or importing `netscope.ui.cli` from the new UI module, which is backwards.
- No configuration boundary. Timeouts (`2.0`, `5.0`), probe counts (`4`), and target hosts (`PUBLIC_DNS = "1.1.1.1"`, `PUBLIC_CDN_HTTP = "https://www.cloudflare.com/"`) are hardcoded as scattered defaults/module constants across `icmp_probe.py`, `dns_probe.py`, `http_probe.py`, and `cli.py`, rather than centralized in `infrastructure/config.py` (which exists as an empty file).
- No abstraction/seam over the third-party calls (`icmplib.ping`, `dns.resolver.Resolver.resolve`, `httpx.Client.get`) for test substitution. Each probe function calls the third-party library directly inside the function body, so unit-testing `cli.run_once()` without monkeypatching internals is not possible.

### Coupling problems

- `ui/cli.py` is coupled to concrete implementations of every layer (`SqliteStore` directly instantiated with the default path, not injected).
- `diagnosis/engine.py`'s three-way branch (`gw_bad`, `dns_bad`, `cdn_bad`) is coupled to exactly the three probe roles `cli.py` happens to call in that order; it has no way to reason about a different set/number of reference points without a rewrite.

### Platform-specific concerns

- `icmp_probe.ping(privileged=True)` requires elevated privileges on Linux (raw socket) and Administrator rights on Windows for the privileged path; the code has a fallback to `privileged=False`, which uses OS-level unprivileged ICMP sockets where supported — but this has not been exercised on Windows or macOS, only reasoned about. Flag as unverified, not incorrect.
- No platform-specific handling exists yet for gateway auto-detection (the user must supply `--gateway` manually); this isn't a bug, but it's a gap the research architecture didn't explicitly call out either.

### Dependency leakage

- `psutil` is declared in `pyproject.toml` and installed, but is not imported anywhere in `src/`. This is dead dependency weight today — every install pulls in a package the code doesn't use yet.

### Domain logic mixed with infrastructure

- Not found in the probes or persistence layers — those stay correctly on their own side of the boundary (probes: I/O only, thin translation; models: no I/O). The one blur is `ui/cli.py`, which mixes I/O (probing, DB writes), orchestration (call order, which measurements feed the diagnosis engine), and presentation (`print(...)`) in a single function. This isn't "domain logic in infrastructure" so much as "no separation between orchestration and presentation" — see "missing boundaries."

### Testability problems

- Zero automated tests exist.
- The pure, deterministic components (`baseline.py`, `experience_score.py`, `diagnosis/engine.py`, `core/models.py`) are fully unit-testable today with no network and no mocks — and are currently untested.
- The probe modules require monkeypatching the underlying library call (`icmplib.ping`, `dns.resolver.Resolver.resolve`, `httpx.Client.get`) to be tested deterministically; no dependency-injection seam exists for this yet, so tests would have to patch module-level names, which is workable but brittle.
- `ui/cli.py`'s `run_once()` cannot be tested without either real network access or monkeypatching four different modules at once, because of the coupling described above.

---

## STEP 3 — RESEARCH COMPLIANCE

| Dependency | Version | License | Why selected (per research doc) | Matches research? | Compatible with MIT? | Attribution required? |
|---|---|---|---|---|---|---|
| `icmplib` | 3.0.4 | LGPL-3.0-or-later (verified via `pip show`, and cross-checked against the installed package's own `Host`/exception API) | Named explicitly in research §4 as the preferred ICMP library over `ping3` (more accurate, has async, safer unprivileged mode) | Yes | Yes, as an unmodified dynamic dependency. LGPL permits this; it would *not* permit vendoring/modifying the library and keeping the whole thing MIT. | Yes — done correctly in `NOTICE`. |
| `dnspython` | 2.8.0 | ISC License (verified via `pip show`) | Named explicitly in research §4 as "the de-facto standard" for DNS in Python | Yes | Yes, ISC is a permissive license functionally equivalent to MIT | Not strictly required, but present in `NOTICE` — fine. |
| `httpx` | 0.28.1 | BSD-3-Clause (verified via `pip show`) | Named explicitly in research §4 for async-capable HTTP probing | Yes | Yes | Present in `NOTICE` — fine. |
| `psutil` | 7.2.2 installed / `>=6.0.0` declared | BSD-3-Clause (verified via `pip show`) | Named in research §4 for NIC/system info | Declared correctly per research, but **not yet used anywhere** — research compliance is only partial: the dependency was added ahead of any code that needs it. | Yes | Present in `NOTICE` — fine, though currently attributing an unused dependency. |
| GeoIP (MaxMind GeoLite2) | not added | N/A | Research §4 flagged this as needing special handling (data EULA, not just code license) | No code exists yet, so nothing to check — but the earlier concern was correctly *not* forgotten: `NOTICE` already pre-emptively documents the GeoLite2 EULA requirement for whenever this is added. | Code: yes (Apache-2.0). Data: **not a simple MIT-compatible permissive license** — separate EULA, registration required. | Yes, explicit attribution string is mandated by MaxMind and already drafted in `NOTICE`. |
| ASN (pyasn / ipwhois) | not added | N/A | Research §4 named `pyasn` (BSD-2-Clause) as preferred | Not yet implemented — `routing/` and `infrastructure/` are empty | Would be yes | Not yet applicable |
| UI (`textual`) | not installed, declared as optional extra `>=0.60.0` | MIT | Research §4 named Textual as the MVP UI choice | Yes, and correctly scoped as an optional extra rather than a hard dependency, which is *more* conservative than the research doc strictly required | Yes | Present in `NOTICE`. |

**Do not assume the previous implementation's license analysis is correct — it was independently re-verified here** via `pip show` against the actually-installed packages and, for `icmplib`, by reading its installed source directly (`icmplib.exceptions.SocketPermissionError` and the `Host` class API used in `icmp_probe.py` were both confirmed to exist as called). No discrepancy was found between what the code/NOTICE claims and what is actually installed.

---

## STEP 4 — LICENSE AUDIT

### License Audit

| Dependency | Version | License | Compatible with MIT project? | Attribution required? | Action |
|---|---|---|---|---|---|
| icmplib | 3.0.4 | LGPL-3.0-or-later | Yes, as unmodified dynamic dependency | Yes | None — already correctly attributed in `NOTICE`. |
| dnspython | 2.8.0 | ISC | Yes | Recommended, not required | None. |
| httpx | 0.28.1 | BSD-3-Clause | Yes | Recommended, not required | None. |
| psutil | 7.2.2 | BSD-3-Clause | Yes | Recommended, not required | **Decide:** either wire it into an actual feature soon, or remove it from `pyproject.toml` until it's used, so the dependency list doesn't overstate what the code does. Not a license problem — a hygiene problem. |
| textual (optional) | not installed | MIT | Yes | Recommended, not required | None — correctly left as an optional extra. |
| pytest (dev optional) | not installed | MIT | Yes | No | None — dev-only, never shipped. |
| GeoLite2 data (MaxMind) | not added | Separate MaxMind EULA (not MIT/Apache/BSD/ISC) | **Conditionally** — permitted for use, but requires account registration, redistribution restrictions, and cannot be treated as a normal permissive dependency | Yes, explicit attribution string mandated | No action needed now (not added); when it is added, gate it as its own reviewable task (see STEP 11) rather than folding it into an unrelated change. |
| MTR / WinMTR / SmokePing / Netdata / Scapy / ntopng / LibreNMS / Nmap source | N/A — not a dependency | GPL-2.0, GPL-3.0, GPL, GPL-3.0+, GPL-2.0, GPL-3.0, GPL-3.0, NPSL respectively | N/A — **no code from any of these was copied**, confirmed by inspecting every implementation file's contents against what these projects' documented approaches look like; all logic in `probes/`, `intelligence/`, `diagnosis/` is original | N/A | None — `NOTICE` already states these were "studied for research/design purposes only," which matches what was actually done. |

**Nothing in the current codebase requires a change to `LICENSE` or `NOTICE` themselves.** The only actionable item is the unused `psutil` dependency, which is a hygiene/compliance-with-research issue, not a license-compatibility issue.

---

## STEP 5 — DOMAIN MODEL AUDIT

### Data Model Gaps

The following things must eventually be representable, and their current status:

| Concept | Current status | Gap |
|---|---|---|
| Latency, packet loss, jitter | Present on `RawMeasurement` (`latency_ms`, `packet_loss_pct`, `jitter_ms`) | None for these three fields specifically. |
| DNS timing | Captured as generic `latency_ms`; resolver identity and answers are stuffed into the untyped `extra: dict` | No typed `resolver_used`, `record_type`, or `answers` fields — currently stringly-typed via `extra`. |
| TCP timing | `ProbeType.TCP` exists in the enum | No probe module exists (`probes/tcp_probe.py` is missing entirely), and no dedicated fields (e.g. `connect_time_ms`) exist on `RawMeasurement`. |
| TLS timing | `ProbeType.TLS` exists in the enum | Same as TCP — enum value only, no probe, no dedicated fields (e.g. `handshake_time_ms`, `cert_expiry`). |
| HTTP timing | Present via `latency_ms` + `extra["status_code"]` | No distinction between DNS+connect+TLS+TTFB+full-body phases — currently one number that (per STEP 7) actually measures full-body time, not TTFB as the docstring claims. |
| Traceroute / hops | `RouteHop` and `RouteSnapshot` dataclasses already exist in `core/models.py` | No probe produces them yet (`routing/` is empty); the models exist ahead of the code that would populate them, which is reasonable sequencing, not a defect. |
| ASN | `RouteHop.asn: Optional[str]` field exists | No dedicated ASN/Organization model — just a bare string field, no linkage to a lookup source. |
| Organization, country, approximate distance | Not present anywhere | No fields on `RouteHop` or any other model for organization name, country, or geo-distance. Must be added when GeoIP/ASN probes are implemented. |
| Service checks (grouping icmp+dns+http as one logical "round" for a named service) | Not present | `RawMeasurement` is a flat, single-probe record; nothing currently groups a batch of measurements under a named "service" (e.g. "Netflix," "generic CDN") the way research Differentiator #6 (Service Intelligence) requires. |
| Historical measurements | Persisted via `SqliteStore` | Works for `RawMeasurement` only; no schema for historical baseline values or historical `Incident`/`ExperienceEvent` records. |
| Baseline | `UserBaseline`/`MetricBaseline` fully modeled | **In-memory only.** Since the CLI is a new process per invocation, a baseline can never accumulate the 5+ samples it needs to become active — this is a functional gap, not just a "nice to have." |
| Incidents | `core.models.Incident` dataclass exists | Never instantiated anywhere in the codebase. Also duplicates `diagnosis.engine.Diagnosis` (see STEP 2) — the two need to be reconciled into one model before either is built out further. |
| Diagnosis | `diagnosis.engine.Diagnosis` dataclass exists and is used | Redundant with `core.models.Incident`, as above. |
| Evidence | Represented only as `list[str]` free-text inside `Diagnosis`/`Incident` | No structured `Evidence` type (e.g. metric name, observed value, baseline value, deviation, source measurement id) — currently unstructured text, which limits future auditability/explainability. |
| Confidence | `confidence_pct: float` present on both `Diagnosis` and `Incident` | Present, but currently hand-assigned constants (`80.0`, `70.0`, `65.0`, `90.0`) in `diagnosis/engine.py` rather than derived from actual signal strength (e.g. baseline deviation sigma) — a modeling gap tied to STEP 6's finding that baseline isn't consulted. |

---

## STEP 6 — INTELLIGENCE AUDIT

Research-intended pipeline:

```
Raw Measurement → Derived Metric → Baseline Comparison → Evidence → Diagnosis → Explanation
```

### What actually happens today

Two separate, shallower pipelines exist, and neither implements the full chain:

**Pipeline A (scoring):**
```
Raw Measurement → Experience Score
                   (static thresholds, no Baseline Comparison step)
```

**Pipeline B (diagnosis):**
```
Raw Measurement → Diagnosis
                   (static thresholds, no Derived Metric or Baseline
                    Comparison step, "Evidence" is just descriptive
                    strings generated after the fact rather than the
                    input to the decision)
```

`intelligence/baseline.py` implements the "Baseline Comparison" stage correctly and in isolation, but it sits outside both pipelines — it is not called by `experience_score.py`, not called by `diagnosis/engine.py`, and not called by `ui/cli.py`. Functionally, **the Baseline Comparison stage does not exist in the running system today**, even though the code for it exists.

### Responsibilities incorrectly mixed

- In `diagnosis/engine.py`, "Evidence" and "Diagnosis" are produced in the same function call, from the same `if` branches, rather than evidence being collected first and diagnosis being a separate step that consumes it. This makes it structurally impossible today to show evidence for a case where no diagnosis was reached, or to unit-test evidence-gathering independently from cause-selection.
- `explanation/explainer.py` is the one component that correctly stays out of this mixing — it only consumes an already-final `Diagnosis`.

This audit does not rewrite these components (per STEP 10/STEP 11 process — decisions and small tasks only).

---

## STEP 7 — NETWORK ENGINEERING AUDIT

| Area | Finding |
|---|---|
| ICMP | `icmp_probe.ping()` correctly falls back from privileged to unprivileged sockets on `SocketPermissionError`. Default `count=4` is a small sample — `icmplib`'s own `jitter` calculation needs at least 2 responses, and `intelligence/baseline.py` needs 5+ samples before it will flag anything as anomalous; a single 4-packet CLI run can never by itself feed the baseline to usefulness. |
| DNS | `dns_probe.resolve()` correctly uses `Resolver.timeout`/`Resolver.lifetime` (verified against the installed `dnspython` 2.8.0 API) and correctly allows pointing at a specific resolver IP for local-vs-public comparison. Single query per call — no retry, no distinction between "resolver unreachable" and "NXDOMAIN," both flatten to the same `error` string. |
| HTTP | **Misleading metric, confirmed by reading the code**: the docstring says "Measures time-to-first-byte style latency," but the implementation calls `client.get(url)` synchronously and times the whole call, which includes full response body download, not just TTFB. For a large response this overstates "latency" versus what the docstring promises. |
| Gateway handling | `diagnose()`'s `is_bad(None)` returns `False`, so an untested gateway (`--gateway` not supplied) is treated identically to a healthy gateway. This is a real false-positive risk: the STEP 1 smoke test produced "ruled out: local network issue" language without ever measuring the local network. |
| Error handling | All three probes use a blanket `except Exception as exc: ... error=str(exc)`. This does not catch `KeyboardInterrupt`/`SystemExit` (both derive from `BaseException`, not `Exception`, so that part is safe), but it does discard exception *type* information, so `diagnosis/engine.py` (which never even reads `.error` today) has no way to eventually distinguish "DNS server unreachable" from "DNS server returned NXDOMAIN" from "connection refused." |
| Timeouts | Consistent units (seconds) across all three probes, but the actual numbers (`2.0`, `2.0`, `5.0`) are hardcoded as function-default arguments in three different files rather than centralized — see STEP 2 "missing boundaries." |
| Packet loss / latency units | Internally consistent (`packet_loss_pct` as 0–100 float, `latency_ms` as milliseconds float) across all three probes and `core/models.py` — no unit inconsistency found. |
| Blocking operations | All probe calls are synchronous and block the calling thread; `cli.run_once()` runs them sequentially, so one invocation takes the sum of all probe timeouts/durations. Not a bug for a one-shot CLI, but incompatible as-is with a future continuously-updating TUI without either threading or moving to the async variants these libraries already offer (`icmplib` has `async_ping`, `httpx` has `AsyncClient`, `dnspython` has `dns.asyncresolver`). |
| Insufficient samples | Both the ICMP probe (4 packets) and the "one call = one data point" pattern used by `cli.run_once()` produce too few samples per invocation for the statistical baseline in `intelligence/baseline.py` to ever activate in practice (see "ICMP" row above and STEP 5's "Baseline" gap). |
| Platform-specific behavior | Not exercised outside this Linux container; the privileged/unprivileged ICMP fallback path is reasoned-about but unverified on Windows/macOS. |

---

## STEP 8 — PRIVACY AUDIT

- **Local-first is honored in the current code.** The only network calls made are to the probe targets the user (or the hardcoded `PUBLIC_DNS`/`PUBLIC_CDN_HTTP` constants) specifies — there is no telemetry endpoint, no analytics call, and no "phone home" behavior anywhere in `src/`.
- **Public IP handling:** not implemented yet — no code queries "what is my public IP." Nothing to audit.
- **DNS data:** `dns_probe.resolve()` queries either the system's configured resolver or an explicitly given one; results are stored in `extra["answers"]` inside the local SQLite file only. Not transmitted anywhere beyond the DNS query itself, which is inherent to performing a DNS lookup at all.
- **Network interface information:** not implemented — `psutil` is a declared but unused dependency (see STEP 4), so there is no interface enumeration happening yet to audit.
- **Gateway information:** the gateway IP is supplied manually via `--gateway`; nothing auto-detects or transmits it beyond pinging it directly.
- **Service targets:** `PUBLIC_DNS = "1.1.1.1"` and `PUBLIC_CDN_HTTP = "https://www.cloudflare.com/"` are hardcoded in `ui/cli.py`, not configurable without editing source (ties back to the missing configuration boundary in STEP 2).
- **Stored data:** `SqliteStore` writes every measurement to `~/.netscope/netscope.db`. No encryption, no automatic expiry/retention policy — not a privacy leak (nothing leaves the machine), but worth a future decision on retention/size growth.
- **Telemetry:** none exists. Per instructions, none was added during this audit.

**Conclusion: no violation of the local-first principle was found in the current implementation.**

---

## STEP 9 — TESTING AUDIT

- **What is actually tested:** nothing, in the automated sense. `tests/` is empty.
- **What was only smoke-tested:** everything. The previous session's manual `python -c "..."` run exercised `baseline.py`, `experience_score.py`, `diagnosis/engine.py`, `explanation/explainer.py`, and `persistence/sqlite_store.py` once each with hand-picked inputs, and separately ran the real `netscope` CLI once. None of this is captured as a repeatable, automated test.
- **What requires real internet connectivity today:** `ui/cli.py`'s `run_once()` as a whole, and by extension anything that imports it, because `probes/icmp_probe.py`, `probes/dns_probe.py`, and `probes/http_probe.py` all call their underlying libraries directly with no substitution seam.
- **What can be deterministically tested right now, with zero mocks:** `core/models.py` (plain dataclasses), `intelligence/baseline.py` (pure math), `intelligence/experience_score.py` (pure function over `RawMeasurement` inputs), `diagnosis/engine.py` (pure decision logic over `RawMeasurement` inputs), `persistence/sqlite_store.py` (using a temp-directory DB path, no network required).
- **What needs mocks/fakes:** the three probe modules, to test their success/failure/error-handling paths without depending on real network conditions (e.g. simulating `icmplib.exceptions.SocketPermissionError`, a DNS timeout, or an HTTP 500).
- **Missing unit tests:** all of the above — currently zero coverage anywhere.
- **Missing integration tests:** an end-to-end `run_once()` test against fake/injected probes (impossible today without monkeypatching, due to the missing seam noted in STEP 2).

No tests were added during this audit, per instructions.

---

## STEP 10 — DECISION

| Component | Decision | Reasoning |
|---|---|---|
| `core/models.py` | **MODIFY** | Structurally sound and worth keeping, but `Incident` needs to be reconciled with `diagnosis.engine.Diagnosis` (pick one), and an `Evidence` type should eventually replace free-text evidence lists. |
| `probes/icmp_probe.py` | **KEEP** | Correct library usage (verified against installed `icmplib` source), correct fallback behavior, correct error containment. No structural problem. |
| `probes/dns_probe.py` | **KEEP** | Correct library usage (verified against installed `dnspython` API). No structural problem. |
| `probes/http_probe.py` | **MODIFY** | Correct as an HTTP probe, but the TTFB claim in its docstring is inaccurate for what it actually measures; either the docstring or the implementation (stream to first byte) needs to change. |
| `intelligence/baseline.py` | **MODIFY** | The math (Welford's algorithm) is correct and should be kept as-is. What needs to change is *integration*: it must be persisted across process runs and actually consulted by scoring/diagnosis. |
| `intelligence/experience_score.py` | **REWRITE** | Currently static-threshold-based, the exact pattern the research document identified NetScope should avoid (see research doc Differentiator #2 and the Zabbix/Icinga row). Needs to consult `UserBaseline` instead of (or in addition to) fixed bounds. |
| `diagnosis/engine.py` | **REWRITE** | Same static-threshold problem as above, plus: duplicates `core.models.Incident`, treats untested inputs as healthy (false-positive risk), discards probe error-type information, and mixes evidence-collection with cause-selection in one step. The general three-way localization *concept* (local vs. ISP vs. destination) is correct and should be preserved — only the implementation needs to change. |
| `explanation/explainer.py` | **KEEP** | Correctly decoupled, minimal, matches its intended architectural role exactly. |
| `persistence/sqlite_store.py` | **MODIFY** | Genuinely local-first (keep that property) and correct for what it currently stores, but the schema needs new tables (baselines, incidents) and a migration strategy before those features can work. |
| `ui/cli.py` | **REWRITE** (architecturally) | The individual steps it calls are fine; the problem is that it *is* the orchestration layer by default, with no `core/use_cases.py` boundary underneath it. A second UI cannot be added cleanly without this changing first. |
| `routing/`, `monitoring/`, `reporting/`, `intelligence/services/` | **KEEP** (as empty scaffolding) | Harmless placeholders matching the intended architecture; no action needed until their corresponding features are built. |
| `infrastructure/` | **KEEP**, but prioritize | Should be the next package to receive real content (a `config.py`), since scattered hardcoded configuration (targets, timeouts, sample counts) is one of the more concrete problems found in this audit. |
| `pyproject.toml` | **MODIFY** | Remove or justify the unused `psutil` dependency. Everything else in the file is correct and matches the research document. |
| `tests/` | **REWRITE** (i.e., build for real) | Currently empty; the pure components are fully testable today with no blockers. |

---

## STEP 11 — RECOMMENDED NEXT STEPS

Ordered, small, independently reviewable tasks. None of these are implemented in this document.

1. **TASK-003 — Add unit tests for the pure components that already exist**
   Cover `core/models.py`, `intelligence/baseline.py`, `intelligence/experience_score.py`, and `diagnosis/engine.py` as they currently stand, with no network required. Establishes a safety net *before* any of the REWRITE/MODIFY tasks below touch this logic.

2. **TASK-004 — Resolve the `Diagnosis` vs. `Incident` model duplication**
   Decide on one model in `core/models.py` for "a diagnosed problem with evidence and confidence," used by both `diagnosis/engine.py` and persistence. No behavior change, just model consolidation.

3. **TASK-005 — Introduce `infrastructure/config.py`**
   Centralize the currently-scattered constants (probe timeouts, sample counts, `PUBLIC_DNS`, `PUBLIC_CDN_HTTP`) into one configuration object/module, without changing probe behavior.

4. **TASK-006 — Add a substitution seam for probe libraries**
   Introduce a minimal interface/protocol (or dependency-injectable callables) so `icmp_probe.ping`, `dns_probe.resolve`, and `http_probe.fetch` can be faked in tests without monkeypatching third-party internals.

5. **TASK-007 — Add integration tests for `ui/cli.run_once()` using the seam from TASK-006**
   Verify orchestration order and error propagation deterministically, without real network access.

6. **TASK-008 — Persist `UserBaseline` in SQLite**
   Extend `persistence/sqlite_store.py` with a `baselines` table and load/save methods, so baseline state survives across CLI invocations. No change to `baseline.py`'s math.

7. **TASK-009 — Wire `intelligence/baseline.py` into `experience_score.py`**
   Replace (or supplement) the fixed `LATENCY_GOOD_MS`/`LATENCY_BAD_MS`/`LOSS_BAD_PCT` thresholds with baseline-relative scoring, using the now-persisted baseline from TASK-008.

8. **TASK-010 — Wire `intelligence/baseline.py` into `diagnosis/engine.py`**
   Same as TASK-009, applied to the diagnosis engine's `is_bad()` logic, and fix the "untested input treated as healthy" false-positive found in STEP 7.

9. **TASK-011 — Extract an application/use-case layer out of `ui/cli.py`**
   Move the probe → score → diagnose → explain → persist orchestration into a new `core/use_cases.py` (as originally proposed in the research architecture), leaving `ui/cli.py` as a thin presentation layer that calls it. This unblocks adding the Textual UI later without duplicating orchestration logic.

10. **TASK-012 — Decide the fate of the `psutil` dependency**
    Either implement the network-interface-info use case it was added for, or remove it from `pyproject.toml`. Small, low-risk, purely a hygiene fix, but should be its own reviewable commit rather than folded into an unrelated change.

**Do not implement TASK-003 through TASK-012 yet — they are listed for review and prioritization only.**
