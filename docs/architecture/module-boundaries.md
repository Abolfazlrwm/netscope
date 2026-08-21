# NetScope — Module Boundaries

**Status:** Proposed (documentation only)

This document defines exactly what each *conceptual* module owns, at a finer grain
than the five top-level packages in `architecture-overview.md` §2–§3. Several
conceptual modules share one top-level package (e.g. Intelligence and Diagnosis both
live under `core/`) — that's intentional, per ADR-001: the package split is by
dependency direction, not topic, but the topics themselves still need clear,
individually-documented boundaries so two pure modules sharing a package don't blur
into each other the way `experience_score.py` and `diagnosis/engine.py` did in the
audited MVP (two independent, duplicate threshold systems, per
`implementation-audit.md` STEP 2).

---

## Discovery

**Lives in:** `adapters/discovery.py`

**Responsibilities:** Find *what* to measure before any probe runs — the default
gateway IP, the active network interface, configured DNS servers, and (best-effort)
connection type (Wi-Fi/Ethernet/cellular where the OS exposes it).

**Must NOT do:**
- Must not run any probe itself (no pinging, no DNS queries) — it only *identifies*
  targets, it does not measure them.
- Must not decide whether a discovered gateway is "healthy" — that's Diagnosis's job.
- Must not cache results indefinitely without a way to re-discover (network
  configuration changes between rounds, e.g. a laptop switching from Wi-Fi to
  Ethernet).

**Inputs:** None (queries the local OS/`psutil` directly).

**Outputs:** A small discovery result (gateway IP, interface name, DNS server list) —
not yet a formal domain model; likely a plain dataclass in `core.models` (e.g.
`NetworkContext`) that Discovery populates and `app` passes to Measurement.

**Dependencies:** `psutil` (per `adr-006-dependency-policy.md`, this is the module
that finally gives `psutil` a purpose, resolving the audit's "unused dependency"
finding).

**Testing strategy:** Mostly requires the real OS/`psutil` to test meaningfully (there
is only one real gateway on a test machine) — best covered by a small number of
manual/opt-in integration tests, plus unit tests for any pure post-processing logic
(e.g. picking the "primary" interface when `psutil` reports several) using
hand-constructed `psutil`-shaped fake data.

---

## Measurement

**Lives in:** `adapters/probes/` (I/O — actually running probes) with domain types
(`Measurement`, `ProbeErrorType`) in `core/models.py`

**Responsibilities:** Run one probe (ICMP/DNS/TCP/TLS/HTTP/traceroute) against one
target and return a `core.models.Measurement`. Translate library-specific results and
exceptions into NetScope's normalized model and structured error type
(`architecture-overview.md` §6). One file per probe type.

**Must NOT do:**
- Must not decide "ISP problem" or any diagnosis-shaped judgment — explicitly called
  out in the task's core architectural principle, and enforced structurally by
  `Measurement` having no field for it.
- Must not read or write baseline/persistence state — a probe returns a value, it
  does not compare it to history.
- Must not know about `Diagnosis`, `Evidence`, `Incident`, or any type from those
  modules — a probe's only awareness is of the target it was asked to measure.

**Inputs:** A target (string address/hostname) plus probe-specific options
(timeout, sample count, resolver IP for DNS, etc.).

**Outputs:** One `core.models.Measurement` per invocation (or `RouteSnapshot` for
the traceroute probe specifically).

**Dependencies:** `icmplib`, `dnspython`, `httpx`, stdlib `socket`/`ssl`
(see `dependency-strategy.md`).

**Testing strategy:** Unit tests with the underlying library call monkeypatched/
dependency-injected to exercise success, timeout, and each mapped
`ProbeErrorType` deterministically, with no real network; a small number of
manual/opt-in "real network" tests for confidence, not run in CI by default.

---

## Routing

**Lives in:** `core/routing.py` (pure analysis) + the `TracerouteProbe` adapter under
`adapters/probes/` (raw hop collection, categorized under Measurement above, not here)

**Responsibilities:** Given one or more `RouteSnapshot`s over time, detect route
changes (via `RouteSnapshot.signature()`, already implemented and characterized by
TASK-003's tests) and compute route-stability signals (churn frequency, hop-level
latency/loss trends) as `DerivedMetric`-shaped output for Diagnosis to consume.

**Must NOT do:**
- Must not run traceroute itself — that's Measurement's job; Routing only analyzes
  `RouteSnapshot`s it's given.
- Must not decide the final diagnosis classification — it produces evidence-shaped
  signals (e.g. "route changed 3 times in the last hour"), Diagnosis decides what
  that means.
- Must not perform ASN/GeoIP lookups itself — those are separate adapter
  responsibilities (a `lookups.py` adapter, categorized under Measurement) whose
  results Routing may receive as already-populated `Hop.asn`/`organization`/`country`
  fields, not something it fetches.

**Inputs:** A sequence of `RouteSnapshot`s for one target, ordered by time.

**Outputs:** Route-stability `DerivedMetric`s / `Evidence` candidates (e.g. churn
count, per-hop latency trend) — precise shape defined when `TASK-021 Route analysis`
is implemented, not finalized in this document.

**Dependencies:** `core.models` only. No third-party library, no I/O.

**Testing strategy:** Pure unit tests with hand-constructed `RouteSnapshot` sequences
— fully deterministic, no network, following the same pattern as TASK-003's
`test_baseline.py`.

---

## Intelligence

**Lives in:** `core/baseline.py` (existing, algorithm unchanged) + `core/scoring.py`

**Responsibilities:** Learn a personal baseline per target/metric
(`MetricBaseline`/`UserBaseline`, Welford's algorithm, already implemented and
characterized) and compute baseline-relative derived metrics/experience scores from
`Measurement`s.

**Must NOT do:**
- Must not decide a diagnosis classification or write human-readable text — those
  belong to Diagnosis and Explanation respectively; today's `experience_score.py`
  mixing "is this bad" with "what caused it" was exactly the boundary the audit found
  crossed, and this module boundary exists specifically to keep them apart going
  forward.
- Must not persist itself — baseline *state* is persisted by `persistence`, loaded/
  saved by `app`; `core/baseline.py`'s classes remain plain in-memory objects that
  `app` is responsible for round-tripping through a `BaselineRepository` port.
- Must not use static, hand-picked thresholds as the *final* scoring mechanism (the
  audit's core complaint about today's `experience_score.py`) — the future scoring
  engine (`architecture-overview.md` §10) must consult the baseline, not bypass it.

**Inputs:** `Measurement`s (to observe/update baseline) and a loaded `UserBaseline`
(to compare against).

**Outputs:** Deviation-sigma values, derived metric scores, and an overall experience
score/level — feeding into Diagnosis as `Evidence`.

**Dependencies:** `core.models` only. No third-party library, no I/O — confirmed
already true of today's `intelligence/baseline.py` by the implementation audit.

**Testing strategy:** Pure unit tests — TASK-003's `test_baseline.py` and
`test_experience_score.py` already demonstrate this is fully achievable
deterministically with no network, and remain valid after the module relocates from
`intelligence/` to `core/` (a file move, not a behavior change).

---

## Diagnosis

**Lives in:** `core/diagnosis.py`

**Responsibilities:** Convert `Evidence` (built from Measurement + Routing +
Intelligence outputs together) into a `Diagnosis`: a classification (one of the
hypothesis enum in `architecture-overview.md` §11), confidence, supporting evidence,
and explicitly what was *not* tested.

**Must NOT do:**
- Must not collect evidence and select a cause in the same function — the audit's
  STEP 6 finding was exactly this conflation in today's `diagnosis/engine.py`; this
  boundary requires evidence-collection and cause-selection to be separately callable
  and separately testable.
- Must not treat an absent `Measurement` (never attempted) the same as a healthy one
  — the audit's headline bug. `INSUFFICIENT_EVIDENCE` exists in the hypothesis enum
  specifically so "not tested" has somewhere to go that isn't silently "fine."
- Must not format human-readable text — that's Explanation's job, kept as a separate
  module so wording can change independent of diagnostic logic (this boundary already
  existed correctly in the audited MVP's `explanation/explainer.py` and is preserved
  here unchanged).
- Must not persist itself — `app` persists the `Diagnosis` it receives back via a
  repository port.

**Inputs:** A list of `Evidence`, itself built from `Measurement`/`RouteSnapshot`/
baseline-comparison outputs.

**Outputs:** A `Diagnosis` (classification, confidence, evidence, ruled-out
hypotheses, timestamp).

**Dependencies:** `core.models` only.

**Testing strategy:** Pure unit tests over hand-constructed `Evidence` lists —
following the exact pattern TASK-003 already used for today's `diagnosis/engine.py`
(including the explicit `test_untested_gateway_currently_behaves_as_healthy`
regression test, which the future implementation must make fail, i.e. fix, not
preserve).

---

## Monitoring (Incident Detection)

**Lives in:** `core/incidents.py` (state machine logic) + scheduling in `app`

**Responsibilities:** Watch `Diagnosis` results across multiple measurement rounds
and open/close `Incident`s when a classification is sustained beyond a single noisy
sample (directly addressing Phase 1 research's Differentiator #5 — incidents require
multiple corroborating signals, not one bad ping, unlike classic threshold-alerting
tools).

**Must NOT do:**
- Must not run measurements itself — it only consumes `Diagnosis` results `app`
  hands it after each round.
- Must not decide *what* the problem is — that's already decided by the `Diagnosis`
  it receives; Monitoring only decides *whether a diagnosis is sustained enough to
  become an incident*.
- Must not persist itself directly — `app` persists `Incident`s via a repository port,
  same pattern as Diagnosis/Intelligence.

**Inputs:** A stream of `Diagnosis` results over time, plus the currently-open
`Incident` (if any) for the same target/service.

**Outputs:** `Incident` open/update/close events.

**Dependencies:** `core.models` only.

**Testing strategy:** Pure unit tests feeding a synthetic sequence of `Diagnosis`
results (e.g. "3 consecutive `ISP_ACCESS_ISSUE` diagnoses 5 minutes apart" →
Incident opens; "1 bad diagnosis surrounded by healthy ones" → no Incident) — fully
deterministic, no timing dependency needed since timestamps are passed in, not read
from the wall clock, inside `core`.

---

## Persistence

**Lives in:** `persistence/`

**Responsibilities:** Implement the repository ports (`MeasurementRepository`,
`RouteRepository`, `BaselineRepository`, `ServiceRepository`, `IncidentRepository`,
per `adr-005-persistence-strategy.md`) using SQLite. Own schema, indices, and any
future migration mechanism entirely.

**Must NOT do:**
- Must not contain any diagnostic/scoring/baseline *logic* — it stores and retrieves
  values `core` already computed; it does not recompute or reinterpret them.
- Must not be imported by `core` — dependency direction is one-way: `core` defines
  the port, `persistence` implements it, never the reverse.
- Must not leak `sqlite3`-specific types (e.g. `sqlite3.Row`) across its own module
  boundary — callers receive `core.models` types back, never raw database rows (a
  gap in today's audited `sqlite_store.py`, whose `recent()` method returns
  `sqlite3.Row` objects directly — noted here as a concrete thing the future
  repository implementation must fix, not preserve).

**Inputs:** `core.models` instances to save; query parameters (target, time range,
limit) to retrieve.

**Outputs:** `core.models` instances (never raw SQL rows) on retrieval.

**Dependencies:** `core.models` (for the types it stores/returns), stdlib `sqlite3`.

**Testing strategy:** Unit tests against a temp-file or in-memory SQLite database
(`sqlite3.connect(":memory:")` or `tempfile`) — no real filesystem path, no network,
following the same local-only pattern the audit already confirmed for today's
`sqlite_store.py`.

---

## Services

**Lives in:** `core.models.Service` (the domain model) + `app`'s aggregation use case
(no dedicated top-level package — see ADR-001's disposition table)

**Responsibilities:** Represent a monitored target generically (name, host/address,
which checks are enabled: icmp/dns/tcp/tls/http) and aggregate the `Measurement`s for
one `Service`'s enabled checks into a per-service view, comparable against a
simultaneous generic-target diagnosis to answer "is this specific service down while
everything else is fine."

**Must NOT do:**
- Must not hardcode any specific provider (GitHub, Cloudflare, Telegram, etc.) into
  `core` — a `Service` is always user- or config-defined data, never a name baked
  into logic, per the task's explicit instruction.
- Must not duplicate probe logic — a `Service`'s checks call the exact same
  `adapters/probes/*` any other measurement round uses; "service monitoring" is a
  grouping/aggregation concern, not a different measurement mechanism.

**Inputs:** A `Service` definition (from configuration or persistence) plus the
`Measurement`s produced by running its enabled checks.

**Outputs:** A per-service `Diagnosis` (via the same Diagnosis module, just scoped to
one `Service`'s measurements) usable for service-vs-general-connectivity comparison.

**Dependencies:** `core.models`, `core.diagnosis` (reused, not reimplemented).

**Testing strategy:** Pure unit tests for the aggregation/comparison logic; the
underlying probes are tested independently under Measurement's own strategy above.

---

## UI

**Lives in:** `ui/`

**Responsibilities:** Presentation only — parse CLI arguments (or render TUI
widgets later), call exactly one `app` use case per user action, format the result
for display.

**Must NOT do:**
- Must not import `adapters` or `persistence` directly — any UI code importing
  `icmplib`, `dnspython`, `httpx`, or `sqlite3` is a boundary violation, per the
  dependency table in `architecture-overview.md` §3.
- Must not contain orchestration logic (deciding which probes to run, in what order,
  what to persist) — that is exactly the bug the implementation audit found in
  today's `ui/cli.py`, and the reason `app` exists as its own package.
- Must not contain diagnostic reasoning — it displays a `Diagnosis`/`Explanation`
  `app` already produced; it does not second-guess or reformat the *substance* of it
  (only presentation formatting, e.g. color/layout).

**Inputs:** User input (CLI args, TUI interactions) and `app` use-case return values.

**Outputs:** Terminal output (text now, Textual widgets later).

**Dependencies:** `app`, `core.models` (for typing/display only), stdlib `argparse`
now, `textual` later (optional extra, per ADR-004).

**Testing strategy:** Thin enough to need few dedicated tests beyond argument-parsing
edge cases; if the Textual TUI grows real interaction/state logic later, add
snapshot-style widget tests at that point, not preemptively.

---

## Cross-module dependency summary

This restates `architecture-overview.md` §3's package-level table at the
conceptual-module grain, for quick reference:

| Module | Package | Depends on (modules) | Never depends on |
|---|---|---|---|
| Discovery | `adapters` | `core.models` | Measurement, Routing, Intelligence, Diagnosis, Monitoring, Persistence, Services, UI |
| Measurement | `adapters` | `core.models` | Routing, Intelligence, Diagnosis, Monitoring, Persistence, Services, UI |
| Routing | `core` | `core.models` | Discovery, Measurement (only consumes their *output* types, never calls them), Persistence, UI |
| Intelligence | `core` | `core.models` | Same as Routing |
| Diagnosis | `core` | `core.models`, Routing's and Intelligence's *output* types | Discovery, Measurement, Persistence, UI |
| Monitoring | `core` | `core.models`, Diagnosis's output type | Discovery, Measurement, Persistence, UI |
| Persistence | `persistence` | `core.models` | Discovery, Measurement, Routing, Intelligence, Diagnosis, Monitoring, UI |
| Services | `core` + `app` | `core.models`, Diagnosis | Persistence, UI directly |
| UI | `ui` | `app` (which depends on all of the above) | Everything else, directly |
