# NetScope — Future Roadmap (TASK-005 → TASK-055)

**Status:** Proposed (documentation only — none of these tasks are implemented by this document)

Each task below is sized to be one focused implementation, a limited set of file
changes, focused tests, one review, and normally one commit — per the task-size rule.
"Dependencies" means *other roadmap tasks that must land first*, not third-party
packages (those are in `dependency-strategy.md`).

## Note on sequencing

The given ordering (005→055) is sound and is kept largely as-is: domain types before
anything that produces/consumes them, probes before the intelligence that reads their
output, intelligence before diagnosis, diagnosis before incidents, persistence after
there's something worth persisting, CLI before UI (UI calls the same `app` use cases
a CLI does, so proving them via CLI first de-risks the UI phase), quality/release last.

One gap is worth flagging rather than silently patching: `architecture-decisions.md`
identifies **configuration centralization** and **logging setup** as needed, but
neither appears as its own numbered task in the given list. Rather than renumbering
all 51 tasks to insert them, they're folded into the two tasks that most naturally
need them first: `TASK-005` (domain entities) gains the `app/` composition-root
skeleton as part of its scope (see below), and `TASK-035` (CLI foundation) gains
config loading and `--verbose` logging setup as part of its scope, since the CLI is
the first place either is actually observable. This is called out explicitly here so
it isn't mistaken for an oversight later.

---

## Phase A — Domain Foundations

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-005 | Domain entities | Establish the `core`/`adapters`/`persistence`/`app`/`ui` package skeleton and the first domain entities | New empty `app/` package (composition-root skeleton only — no wiring yet); relocate/rename existing `core/models.py` content into the new layout without behavior change | none | `src/netscope/app/__init__.py`, package `__init__.py`s for the new layout | Existing 51 tests still pass unmodified (pure relocation) | `refactor: establish core/adapters/persistence/app/ui package skeleton` |
| TASK-006 | Measurement contracts | Define the `Measurement` domain type per `architecture-overview.md` §5 (successor to today's `RawMeasurement`) | `core/models.py` additions/renames only; no adapter changes yet | TASK-005 | `src/netscope/core/models.py` | Unit tests for field defaults/invariants, extending `tests/test_models.py`'s existing pattern | `feat: define Measurement domain contract` |
| TASK-007 | Probe result model | Define `core.ports.Probe` Protocol and the adapter-facing result shape | `core/ports.py` (new) | TASK-006 | `src/netscope/core/ports.py` | Unit tests asserting the Protocol shape via a minimal fake implementation | `feat: define Probe port` |
| TASK-008 | Error model | Implement `ProbeErrorType` enum per `architecture-overview.md` §6 | `core/models.py` | TASK-006 | `src/netscope/core/models.py` | Unit tests for enum completeness/serialization | `feat: add structured ProbeErrorType` |
| TASK-009 | Evidence model | Implement the structured `Evidence` type per `architecture-overview.md` §5, and reconcile `Diagnosis`/`Incident` duplication per the audit | `core/models.py` | TASK-008 | `src/netscope/core/models.py` | Unit tests for `Evidence` construction and the unified `Diagnosis`/`Incident` shape | `feat: add structured Evidence model, resolve Diagnosis/Incident duplication` |

## Phase B — Discovery

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-010 | Local interface discovery | Enumerate network interfaces via `psutil`, giving it its first real use | `adapters/discovery.py` (new) | TASK-005, TASK-007 | `src/netscope/adapters/discovery.py` | Unit tests with fake `psutil`-shaped data; a small opt-in manual integration test | `feat: add network interface discovery` |
| TASK-011 | Gateway discovery | Identify the default gateway per platform | `adapters/discovery.py` | TASK-010 | same file, extended | Unit tests for platform-branch selection logic with mocked `psutil`/OS calls | `feat: add default gateway discovery` |
| TASK-012 | Network type detection | Best-effort Wi-Fi/Ethernet/cellular classification | `adapters/discovery.py` | TASK-011 | same file, extended | Unit tests for classification logic given fake interface data | `feat: add network type detection` |
| TASK-013 | Network metadata | Assemble discovery outputs into a `NetworkContext` domain object | `core/models.py` (new `NetworkContext` type), `adapters/discovery.py` (return it) | TASK-012 | both files | Unit tests for `NetworkContext` construction | `feat: introduce NetworkContext model` |

## Phase C — Probes

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-014 | ICMP | Migrate/re-implement today's `icmp_probe.py` as an `ICMPEchoProbe` adapter implementing `core.ports.Probe`, with structured errors | `adapters/probes/icmp.py` | TASK-007, TASK-008 | new file (relocation + error-model integration of existing logic) | Unit tests with `icmplib.ping` monkeypatched for success/timeout/permission-denied paths | `refactor: migrate ICMP probe to adapter, add structured errors` |
| TASK-015 | DNS | Same migration for DNS | `adapters/probes/dns.py` | TASK-014 (pattern reuse) | new file | Unit tests with `dns.resolver` monkeypatched, covering NXDOMAIN/timeout/success | `refactor: migrate DNS probe to adapter, add structured errors` |
| TASK-016 | TCP | New TCP connect-timing probe, stdlib `socket` only | `adapters/probes/tcp.py` (new) | TASK-007, TASK-008 | new file | Unit tests with `socket.create_connection` monkeypatched for connect success/refused/timeout | `feat: add TCP connect-timing probe` |
| TASK-017 | TLS | New TLS handshake-timing probe, stdlib `ssl`, layered on TASK-016's socket | `adapters/probes/tls.py` (new) | TASK-016 | new file | Unit tests with `ssl.wrap_socket`/`SSLContext` monkeypatched for handshake success/failure | `feat: add TLS handshake-timing probe` |
| TASK-018 | HTTP | Migrate today's `http_probe.py`, and fix the audit-flagged TTFB docstring/implementation mismatch (streaming to first byte, or corrected docstring — pick one explicitly in this task) | `adapters/probes/http.py` | TASK-014 (pattern reuse) | new file | Unit tests with `httpx.Client` monkeypatched; a test asserting the TTFB claim now matches behavior | `refactor: migrate HTTP probe to adapter, fix TTFB metric semantics` |

## Phase D — Routing

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-019 | Traceroute abstraction | `TracerouteProbe` adapter wrapping `icmplib.traceroute()` per ADR-003, including the `PERMISSION_DENIED` handling ADR-003 specifies | `adapters/probes/traceroute.py` (new) | TASK-007, TASK-008 | new file | Unit tests with `icmplib.traceroute` monkeypatched for success and `SocketPermissionError` | `feat: add traceroute probe via icmplib` |
| TASK-020 | Hop model | Finalize `core.models.Hop`/`RouteSnapshot` fields (asn/organization/country placeholders) per `architecture-overview.md` §5 | `core/models.py` | TASK-019 | same file | Unit tests for `Hop`/`RouteSnapshot` construction and `signature()` (already covered by TASK-003's tests — extend, don't replace) | `feat: extend Hop model with lookup-ready fields` |
| TASK-021 | Route analysis | Implement `core/routing.py`: churn detection over a `RouteSnapshot` sequence | `core/routing.py` (new) | TASK-020 | new file | Pure unit tests with hand-constructed `RouteSnapshot` sequences | `feat: add route-churn analysis` |
| TASK-022 | ASN/ISP intelligence | `adapters/lookups.py` ASN lookup adapter (`pyasn`, per `dependency-strategy.md`) | TASK-020 | new dependency `pyasn` added in this task specifically (not before) | `src/netscope/adapters/lookups.py`, `pyproject.toml` | Unit tests with a small local test ASN database fixture | `feat: add ASN lookup adapter (pyasn)` |
| TASK-023 | Distance estimation | Approximate geographic distance from GeoIP data (depends on the GeoLite2 EULA/attribution task being resolved first — see `dependency-strategy.md`) | `adapters/lookups.py`, extended | TASK-022 | same file; `pyproject.toml` (`geoip2` added); `NOTICE` updated if not already sufficient | Unit tests with a small local test GeoIP database fixture | `feat: add GeoIP-based distance estimation` |

## Phase E — Intelligence & Diagnosis

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-024 | Baseline | Relocate `intelligence/baseline.py` to `core/baseline.py` (no algorithm change) and add a `BaselineRepository` port for persistence | `core/baseline.py`, `core/ports.py` | TASK-005, TASK-007 | both files | TASK-003's `test_baseline.py` relocated and still passing unmodified; new tests for the repository port shape | `refactor: relocate baseline to core, add persistence port` |
| TASK-025 | Health Score | Rewrite `experience_score.py` (as `core/scoring.py`) to consult `UserBaseline` instead of static thresholds, per `architecture-overview.md` §10 | `core/scoring.py` | TASK-024 | new file (replacing `intelligence/experience_score.py`) | New characterization-then-behavior tests: TASK-003's existing static-threshold tests are explicitly retired/updated here (this is the task that's allowed to change that documented-as-provisional behavior) | `feat: rewrite experience scoring against personal baseline` |
| TASK-026 | Diagnosis | Rewrite `diagnosis/engine.py` (as `core/diagnosis.py`) as evidence/hypothesis-based, per `architecture-overview.md` §11, explicitly fixing the untested-gateway bug | `core/diagnosis.py` | TASK-009, TASK-025 | new file (replacing `diagnosis/engine.py`) | TASK-003's `test_untested_gateway_currently_behaves_as_healthy` must now fail under the *old* assertion and be rewritten to assert the *fixed* behavior — this is the task that resolves that documented bug | `feat: rewrite diagnosis engine as evidence-based, fix untested-gateway bug` |
| TASK-027 | Evidence generation | Split evidence-collection out of TASK-026's diagnosis function into its own testable unit, per `module-boundaries.md`'s "Diagnosis must not collect evidence and select a cause in the same function" rule | `core/diagnosis.py`, refactored | TASK-026 | same file, restructured | Unit tests for evidence-collection independent of cause-selection | `refactor: separate evidence collection from cause selection` |
| TASK-028 | Incident detection | Implement `core/incidents.py` per `module-boundaries.md`'s Monitoring section | `core/incidents.py` (new) | TASK-027 | new file | Unit tests with synthetic `Diagnosis` sequences (sustained vs. transient) | `feat: add incident detection state machine` |

## Phase F — Persistence

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-029 | SQLite schema | Design the multi-table schema (measurements, routes/hops, baselines, services, incidents, evidence) implementing ADR-005's repository ports | `persistence/schema.py` or equivalent | TASK-009, TASK-020, TASK-024, TASK-028 | new file(s) under `persistence/` | Schema-creation tests against a temp/in-memory SQLite database | `feat: define multi-table SQLite schema` |
| TASK-030 | Measurement persistence | Implement `MeasurementRepository`, migrating today's `sqlite_store.py` logic and fixing the `sqlite3.Row`-leakage issue flagged in `module-boundaries.md` | `persistence/measurement_repository.py` | TASK-029 | new file (successor to `persistence/sqlite_store.py`) | Unit tests against temp-file SQLite; verify no `sqlite3.Row` crosses the repository boundary | `refactor: implement MeasurementRepository, stop leaking sqlite3.Row` |
| TASK-031 | History queries | Implement time-range/target-filtered queries needed by CLI's `history` command | `persistence/measurement_repository.py`, extended | TASK-030 | same file | Unit tests for range/filter query correctness | `feat: add history query methods` |

## Phase G — Services

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-032 | Service targets | Implement the generic `Service` domain model per `architecture-overview.md` §5 / `module-boundaries.md`'s Services section | `core/models.py` | TASK-006 | same file | Unit tests for `Service` construction, no hardcoded providers | `feat: add generic Service model` |
| TASK-033 | Service monitoring | `app`-level use case running a `Service`'s enabled checks and aggregating results | `app/use_cases.py` | TASK-014–018 (all probes), TASK-032 | new/extended file | Unit tests using fake adapters implementing `core.ports.Probe` | `feat: add service monitoring use case` |
| TASK-034 | Service intelligence | Compare a `Service`'s `Diagnosis` against a simultaneous generic-target `Diagnosis` to localize service-specific vs. general issues | `app/use_cases.py`, extended | TASK-026, TASK-033 | same file | Unit tests for the comparison logic with fake `Diagnosis` inputs | `feat: add service-specific vs. general connectivity comparison` |

## Phase H — CLI

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-035 | CLI foundation | Rebuild `ui/cli.py` as a thin presentation layer calling `app` use cases only; **includes config loading (TOML, per `architecture-decisions.md`) and `--verbose` logging setup**, since this is the first place either becomes observable | `ui/cli.py`, `app/composition_root.py` (or similar), `core/config.py`/`app/config.py` (new) | TASK-005 through TASK-034 (needs a working `app` to call) | multiple, as listed | Unit tests for argument parsing; unit tests for `app` use cases using fake adapters/repositories (this is the audit's "untestable orchestration" gap, closed here) | `refactor: rebuild CLI as thin presentation layer, add config and logging` |
| TASK-036 | Diagnostic command | `netscope diagnose` command wired to the diagnosis use case | `ui/cli.py`, extended | TASK-035 | same file | Unit tests for command wiring with fake `app` use case | `feat: add diagnose command` |
| TASK-037 | Route command | `netscope route <target>` wired to traceroute + route analysis | `ui/cli.py`, extended | TASK-035, TASK-021 | same file | Unit tests for command wiring | `feat: add route command` |
| TASK-038 | JSON output | `--json` flag for machine-readable output across commands | `ui/cli.py`, extended | TASK-036, TASK-037 | same file | Unit tests asserting valid JSON structure for each command | `feat: add --json output mode` |
| TASK-039 | Report export | Evidence-report export (Phase 1 research Differentiator #8) | `app/use_cases.py`, `ui/cli.py` | TASK-038 | both files | Unit tests for report content/structure | `feat: add evidence report export` |

## Phase I — UI

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-040 | UI foundation | Textual app skeleton calling `app` use cases (installs the `ui` optional extra) | `ui/tui/app.py` (new) | TASK-035 (proves `app` use cases work via CLI first) | new files under `ui/tui/` | Minimal smoke tests (Textual app instantiates without error); most verification manual at this stage | `feat: add Textual UI foundation` |
| TASK-041 | Dashboard | Main status/overview screen | `ui/tui/screens/dashboard.py` | TASK-040 | new file | Widget-level tests if state logic exists; otherwise manual verification | `feat: add dashboard screen` |
| TASK-042 | Network information | Discovery/`NetworkContext` display screen | `ui/tui/screens/network_info.py` | TASK-040, TASK-013 | new file | Same as TASK-041 | `feat: add network information screen` |
| TASK-043 | Route visualization | Route/hop display screen | `ui/tui/screens/route.py` | TASK-040, TASK-021 | new file | Same as TASK-041 | `feat: add route visualization screen` |
| TASK-044 | Diagnostics UI | Diagnosis/evidence display screen | `ui/tui/screens/diagnostics.py` | TASK-040, TASK-026 | new file | Same as TASK-041 | `feat: add diagnostics screen` |
| TASK-045 | History UI | Historical measurements/incidents screen | `ui/tui/screens/history.py` | TASK-040, TASK-031 | new file | Same as TASK-041 | `feat: add history screen` |
| TASK-046 | Services UI | Service list/status screen | `ui/tui/screens/services.py` | TASK-040, TASK-034 | new file | Same as TASK-041 | `feat: add services screen` |

## Phase J — Quality

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-047 | Unit tests | Fill any coverage gaps left by the per-feature tests above (audit/roadmap review pass, not new features) | `tests/*` | TASK-005 through TASK-046 | various | New/expanded unit tests only | `test: fill unit test coverage gaps` |
| TASK-048 | Integration tests | End-to-end `app` use-case tests with fake adapters/repositories wired together (not real network) | `tests/integration/*` (new) | TASK-035 | new test files | Integration-level tests, still offline via fakes | `test: add end-to-end use-case integration tests` |
| TASK-049 | Cross-platform validation | Manual/CI validation on Windows/Linux/macOS, focused on the two platform-branching adapters (traceroute, discovery) | CI config, manual test notes | TASK-011, TASK-019 | `.github/workflows/*` (new) | Platform-specific smoke tests where CI runners allow | `ci: add cross-platform validation` |
| TASK-050 | Performance/reliability | Timeout/retry tuning, confirm probe timeouts are centrally configurable (TASK-035's config work) | Various adapter files, config | TASK-035, TASK-047 | various | Timing-focused unit tests (mocked delays, not real network latency) | `perf: tune probe timeouts and retry behavior` |

## Phase K — Release

| TASK | Title | Goal | Scope | Depends on | Expected files | Expected tests | Commit suggestion |
|---|---|---|---|---|---|---|---|
| TASK-051 | Documentation | User-facing docs (install, usage, configuration) distinct from the `docs/architecture/` engineering docs this task produces | `docs/usage/*` (new) | TASK-039 | new docs | N/A (docs-only) | `docs: add user-facing usage documentation` |
| TASK-052 | README/screenshots | Update `README.md` with current features, screenshots of the CLI/TUI | `README.md` | TASK-046, TASK-051 | `README.md`, `docs/images/*` | N/A | `docs: update README with current features and screenshots` |
| TASK-053 | Packaging | Verify `pip install`/build artifacts, confirm `pyproject.toml` is release-ready | `pyproject.toml` review, possibly `MANIFEST.in` | TASK-050 | possibly `pyproject.toml` | Build/install smoke test | `build: prepare packaging for release` |
| TASK-054 | CI | Full CI pipeline (lint, test, build) if not already covered by TASK-049 | `.github/workflows/*` | TASK-049, TASK-053 | CI config | N/A (CI itself is the check) | `ci: add full test/build pipeline` |
| TASK-055 | v0.1.0 release | Tag and publish the first release | none (process task) | all of the above | `CHANGELOG.md` entry | N/A | `chore: release v0.1.0` |

---

## Summary

51 tasks (TASK-005 through TASK-055), grouped into 11 phases (Domain Foundations,
Discovery, Probes, Routing, Intelligence & Diagnosis, Persistence, Services, CLI, UI,
Quality, Release), each sized for one focused implementation and one commit. No task
in this document is implemented as part of producing it.
