# Changelog

All notable changes to NetScope are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.9.0] — 2026-11-11

The "confirm before you report" release: a dropped packet is no longer a
verdict. Every ICMP sweep NetScope runs is four packets wide, and one stray
drop on a busy hop read straight into scoring, the baseline and the diagnosis
as 25% loss -- a property of the link, supposedly, measured exactly once. v0.9
treats partial loss as what it actually is, a suspicion: the sweep is re-run
twice, the rounds merge into one window, and what reaches every downstream
verdict is the confirmed reading, with the story of how it was confirmed
attached.

### Added

- **Loss-confirmation policy** (`core/loss.py`, pure domain): 
  `needs_loss_confirmation` names the exact shape worth a second opinion -- a
  successful sweep reporting loss strictly between 0% and 100%. Total failure
  is a finding, not a suspicion: dead targets never get re-swept. A merged
  window carries a `loss_rounds` marker, so the policy is idempotent -- a
  window can never be confirmed into a window of windows.
- **The window itself** (`merge_loss_rounds`): loss averaged across the sweeps
  (the per-round numbers ride along in `extra["loss_rounds"]`, so nothing
  hides behind the average), latency and jitter averaged over the rounds that
  reported them, success if *any* sweep got an answer, RTT min/max and packet
  counts re-aggregated across the window, and `loss_confirmed` -- true when
  the loss reproduced in any sweep after the first, false when every
  confirmation came back clean: a transient, worth knowing it healed.
- **Orchestration where execution lives** (`run_with_loss_confirmation`,
  app layer): both ICMP call sites -- the diagnose round's gateway and
  public-DNS sweeps, and every service check, saved or ad-hoc -- route
  through it. Confirmation costs nothing until there is something to
  confirm: clean sweeps and dead targets run exactly once, byte for byte the
  pre-0.9 path.
- **One shared verdict in the output** (`format_loss_note`, the route
  memory's one-wording pattern): the measurements table's detail column
  appends `loss confirmed across 3 sweeps` or `loss transient across 3
  sweeps`; `--json`'s `extra` block carries `loss_rounds`,
  `loss_confirmed` and `loss_window_rounds` for machines.

### Notes

- The merged window is what the baseline observes: the running loss baseline
  learns the window average from now on, never the raw first suspicion.
- No configuration knob, deliberately: two extra sweeps is the smallest
  number that separates a stray drop from a live problem, and the policy only
  spends them on lossy sweeps -- confirmation is free exactly when it should
  be.

## [0.8.0] — 2026-11-10

The "route memory" release: `netscope route` finally remembers. The
`route_snapshots`/`route_hops` tables ADR-005 drew into the very first
schema existed since day one -- and sat there dead: no writer, no reader,
every run's churn verdict computed over the single snapshot it had just
produced and then thrown away. v0.8 feeds those tables and reads them back,
so the stability verdict now spans runs: "unchanged across the last 7 runs,
same path since Thursday" is evidence; "stable (single snapshot)" was a
non-verdict.

### Added

- **Route history, persisted** (`RouteRepository`, the third of ADR-005's
  five repository ports to land, after the baseline's and the service
  list's): every `netscope route` run -- and every TUI trace -- appends its
  enriched snapshot to the store. No migration was needed: the tables date
  from schema v1, `SCHEMA_VERSION` stays 3, and a v2-era database opens
  as-is; the first run simply starts filling tables that have been waiting
  since the schema was drawn.
- **Churn across the stored window**: the use case loads the target's most
  recent 20 observations (oldest first -- the exact chronological order
  `analyze_route_churn` compares), analyzes `[*history, this run]`, and only
  then appends the new snapshot -- compare-then-observe, the same ordering
  doctrine v0.4 set for throughput baselines: a run must never contaminate
  its own comparison.
- **Hop-level diffs**: a `RouteChange` now records WHICH hops moved
  (`RouteHopChange`, positionally paired -- the same pairing the signature
  compares, so the diff can never disagree with the verdict). A hop that
  answered last run and timed out this run shows as `→ no reply`, not as a
  generic "changed"; a path that grew or shrank diffs at exactly the
  positions the hop lists disagree.
- **`netscope route` verdicts that mean something**: "first observation --
  future runs compare against this path" replaces the old single-snapshot
  "stable" (a certainty the evidence did not support); "unchanged across the
  last N runs -- same path since <date>"; and "changed vs <date> -- hop K:
  <old> → <new>", with the window's change count beneath it when more than
  one path transition is in the window. `--json`'s `stability` block gains
  `prior_runs`, `window_start`, and a `last_change` object carrying the
  FULL hop diff (the human line caps at three hops; machines get every
  moved position).
- **TUI route screen speaks the same language**: the status tail carries
  the compact verdict ("stable across 5 runs" / "changed vs 11-09 10:00 --
  hop 3: ...") from the same shared renderer the CLI uses -- one wording,
  two densities, no drift.

### Notes

- Verdict honesty is enforced by wording, not just intent: `prior_runs == 0`
  (a first observation, or no repository wired) can never print "stable" --
  `route_stability_summary` reserves that word for windows that actually
  contain more than one observation.
- Graceful degradation, v0.7 doctrine: no `RouteRepository` wired -> v0.7's
  single-snapshot behavior, byte for byte, and nothing is stored (an
  automatic observation is not a user-issued save -- there is nothing to
  refuse to lose, unlike the curated service list's explicit writes).
- Lookup enrichment stays outside the comparison: `signature()` is
  address-derived, so an ASN database or reverse-DNS refresh alone can
  never register as a route change -- the guarantee
  `tests/test_routing.py::test_churn_detection_is_unaffected_by_asn_organization_country_changes`
  has pinned since v0.2, now load-bearing for persisted history.

## [0.7.0] — 2026-11-09

The "services that stick" release: the curated service list finally has a
home. v0.6's services board could only show what the log already held;
v0.7 closes TASK-032's storage half -- a service you care about is on the
board before it has any evidence.

### Added

- **Saved services, persisted** (TASK-032's storage half): schema v3 adds
  the `services` table -- keyed by the user-supplied name, one row per
  curated service -- and `ServiceRepository` implements the port ADR-005
  has promised since the schema was first drawn (the second of the five
  expected repository ports to land, after the baseline's). The v2 -> v3
  migration is purely additive: a v2 database opens under v3 with every
  pre-existing row untouched.
- **`netscope service add/list/remove/check`**: save (or update) a
  service by name; list the saved list exactly as persisted, alphabetical
  by name; remove by exact name -- removing a typo'd name says so and
  lists what *is* saved, never a silent success; and run a saved
  service's own curated checks through the same pipeline and
  presentation as `netscope check`, persisting to the same log.
- **Saved services pinned on the TUI board** (`netscope tui`, `s`): a
  curated entry appears on the services screen *before* its first check
  -- dashes for never-measured cells, "never" for last seen -- and a
  host that is both saved and logged shows once, not twice. The board's
  input gains a second grammar: `name host` saves through the same use
  case as `netscope service add` (the host is the last word; hosts never
  contain spaces).
- **`check_service`** use case: the saved-service form of `check_target`
  (which is now a thin wrapper over it) -- an unsaved check IS a one-off
  Service per the domain model; v0.7 is what makes the distinction real.

### Notes

- Upsert, not replace: saving over an existing name uses
  `ON CONFLICT DO UPDATE`, touching exactly the two columns a Service
  actually carries -- never the delete-then-insert of `INSERT OR
  REPLACE`, which would reset anything a future migration adds.
- The check set is stored alphabetically sorted and comma-separated --
  byte-stable rows regardless of Python set iteration order; the CLI and
  the TUI keep their own documented display order.
- Writes refuse to lose data: saving or removing without a
  ServiceRepository wired raises instead of silently dropping the write,
  while listing without one returns an empty list -- reads tolerate
  absence, writes do not.
- The v0.6 TUI ran every SQLite read/write on whichever Textual worker
  thread the pool handed it, against connections created on another
  thread -- legal only because its tests used in-memory fakes. v0.7
  fixes it at the root: `create_connection` opens with
  `check_same_thread=False` (every repository access is a short,
  self-contained transaction, and SQLite ships serialized), so the
  shell's lazily-built container can be used from any worker.
- Removing a service from the board is deliberately absent: removal is
  `netscope service remove`, so the board has no destructive keys.

**Quality gates:** 661 tests passing · ruff clean · mypy clean.


## [0.6.0] — 2026-11-09

The "full picture" release: the TUI outgrows its single screen -- six
screens under one shell, completing Phase I of the roadmap
(TASK-041 -- TASK-046).

### Added

- **Six-screen TUI** (`netscope tui`, requires `pip install
  netscope[tui]`): one shell owns dependency wiring, round execution
  (the same `run_measurement_round` use case the CLI runs), incident
  state, and screen routing. Every screen is installed once and kept
  alive for the session -- switching never discards state, and the
  shell re-delivers its latest round whenever a screen becomes active,
  so no screen ever shows data older than the shell's.
- **Network screen** (`n`, TASK-042): every interface with its type,
  state and addresses, plus the resolved gateway -- the dashboard's
  best-effort card promoted to a full view. Discovery runs on a
  background worker; `r` re-discovers.
- **Route screen** (`p`, TASK-043): traceroute through the same
  `run_route_analysis` use case behind `netscope route` -- reverse-DNS
  hostnames, AS numbers, per-hop loss and RTT, and the churn verdict.
  `enter` traces any host; a failed traceroute renders as failed,
  never as an empty route.
- **Evidence screen** (`e`, TASK-044): the reasoning behind the
  dashboard's one-line summary -- every evidence row (observed /
  expected / deviation / severity), the ruled-out list, and the
  plain-language explanation generated from the same diagnosis object.
- **History screen** (`h`, TASK-045): recent rounds reconstructed from
  the local log with scores recomputed against the current baseline
  (disclosed as such); `enter` opens a round's per-measurement detail.
  Without a measurement repository it says so instead of pretending to
  poll.
- **Services screen** (`s`, TASK-046): a status board of every target
  the log holds -- each target's latest persisted result per service
  check, dashes for never-checked cells, bandwidth rows kept out of
  the check matrix (throughput evidence is not a service-check
  reading). Type a host and its five checks run through `check_target`,
  persist exactly like `netscope check`, and join the board.
- **TUI screenshot regenerated** (`docs/images/tui-dashboard.svg`, via
  `scripts/generate_screenshots.py`) showing the shell's new screen
  keys.

### Notes

- The dashboard keeps its v0.2 content and behavior -- only the
  plumbing moved up into the shell. A round can still land between the
  shell's mount and a screen's own; it is replayed on mount instead of
  dropped (the same buffering keeps early worker-failure lines).
- A curated service list still has nowhere to persist (TASK-032's
  storage half stays open on the roadmap), so the services board
  derives its rows from the measurement log itself rather than
  inventing hidden persistence.

**Quality gates:** 595 tests passing · ruff clean · mypy clean.

## [0.5.0] — 2026-11-08

The "show your work" release: the measurement log becomes a portable,
self-contained evidence document, and the project gains the user-facing
documentation the roadmap always promised next to the architecture notes.

### Added

- **`netscope report`** (TASK-039, evidence-report export): rebuilds the
  persisted measurement log into an honest markdown document -- the
  window's rounds with recomputed scores, per-target evidence tables,
  every failure with its error taxonomy, and the notes that keep the
  numbers honest. `--days N` sets the window, `--target T` scopes the
  export, `--out FILE` writes a UTF-8 file, `--json` emits the canonical
  machine-readable payload.
- **Disclosure over truncation**: per-target tables show the newest 40
  rows and state exactly how many older rows were omitted; the rounds
  and failures sections do the same. Aggregates always cover the full
  window -- an evidence document that hides evidence is not evidence.
- **`MeasurementRepository.targets()`**: every distinct target in the
  log, so the report walks the window as one range query per target
  instead of one unbounded scan.
- **User-facing usage docs** (`docs/usage/`, TASK-051): installation,
  quickstart, the command reference, configuration, and the `--json`
  payload reference -- linked from the README next to the architecture
  deep-dives.

### Notes

- Round scores in the report (and `history`) are recomputed against the
  *current* personal baseline -- the log alone cannot reconstruct the
  baseline as it stood at each round's time, and the document says so
  rather than pretending otherwise.
- Latency means only ever average *successful* latency readings; a
  bandwidth row's v0.4 verdict lives in the verdict column, never as
  latency evidence.

**Quality gates:** 586 tests passing · ruff clean · mypy clean.

## [0.4.0] — 2026-11-05

The "know your normal" release: speed tests now learn your personal
throughput baseline and judge every new run against it.

### Added

- **Throughput baseline** (`UserBaseline.throughput`): every successful
  `netscope speed` run is observed, Welford-exact, into its own
  per-target store -- never into the latency/packet-loss metrics a
  download reading must not pose as (the v0.3 doctrine, now structural).
- **Baseline-relative verdicts** (`compare_throughput`): once the
  baseline has ≥ 3 observations, each new run is compared BEFORE it is
  folded in, and classified against a ±15% band: `as_usual`,
  `slower_than_usual`, or `faster_than_usual` -- percentage-based,
  because "your download is 18% below usual" is the sentence a human
  acts on.
- **`netscope speed` shows it**: the headline panel gains a comparison
  line ("vs your usual 82.1 Mbps · +15.2% — faster than usual"), and
  `--json` gains `baseline_mean_mbps` / `baseline_samples` /
  `deviation_pct` / `verdict` -- keys absent entirely while the
  baseline is still too young to have an opinion (the v0.3 shape).
- **Schema v2**: a dedicated `throughput_baselines` table, additive by
  design -- existing databases upgrade in place on next start, with
  every v1 row untouched.


## [0.3.0] — 2026-11-05

The "how fast does it actually feel" release: NetScope can now measure
download throughput, not just latency.

### Added

- **Download-bandwidth probe** (`ProbeType.BANDWIDTH`): streams a
  configurable endpoint for a bounded window, samples Mbps at fixed
  intervals against a monotonic clock, and reports the **median**
  sample — robust against the TCP slow-start ramp — with the peak
  sample and total bytes moved kept as evidence. Error taxonomy
  matches the HTTP probe (timeout / HTTP failure / unavailable).
- **`netscope speed [--url URL] [--seconds S] [--json]`**: one
  throughput measurement as a standalone command, rendered as a
  headline panel (median Mbps, peak, MB moved, sample count). A
  speed test never runs inside `diagnose` rounds — it is slow and
  data-hungry by nature, and its result is throughput evidence, not
  a latency reading.
- **`bandwidth_url` / `bandwidth_seconds` config keys**: the download
  endpoint (default: Cloudflare's speed endpoint) and the measurement
  window are configuration, like every other target NetScope talks to.
- The probe registry, the `ProbeType` enum, the measurements table's
  detail line, and the adapter/CLI/config test suites all speak
  bandwidth now.

### Notes

- Throughput is reported as evidence only: it does not feed the 0–100
  experience score and is never observed into the personal baseline
  (a bandwidth measurement carries `latency_ms=None` by design, so it
  cannot pose as a latency reading). Baseline-relative throughput
  scoring is a planned follow-up.

## [0.2.0] — 2026-11-02

The "make it real" release: every built-but-unwired capability is now
connected, the CLI grew five commands, and the project gained its TUI
dashboard, CI, and packaging polish.

### Added

- **Live TUI dashboard** (`netscope tui`, requires `pip install
  netscope[tui]`): big experience score, sparkline of recent scores,
  per-probe results table, diagnosis panel, incident log, and network
  interfaces. Rounds run on background workers; `r` forces an
  immediate round; fully offline-testable via Textual pilot.
- **Persistent personal baseline** — the headline differentiator now
  actually persists: `BaselineRepository` (SQLite, exact Welford
  state) wired through the composition root; `netscope diagnose`
  loads, observes, and saves it every round. Your "normal" survives
  restarts.
- **Gateway auto-detection** (`--gateway auto`, now the default):
  cross-platform default-gateway detection (Linux `/proc/net/route`,
  macOS `route`, Windows `route print`) with explicit `none` to skip.
- **`netscope check <host>`** — run any subset of the five service
  checks (`--checks icmp,dns,tcp,tls,http`) against any target.
- **`netscope history`** — recent measurement rounds reconstructed
  from the local log, with per-round experience scores.
- **`netscope monitor`** — continuous rounds with evidence-based
  incident detection: opens only after a 3-round sustained diagnosis,
  closes only after sustained health; single noisy samples never flap.
- **`--json` on every read command** (`diagnose`, `check`, `route`,
  `history`) — one canonical machine-readable wire format built in
  `app/reports.py`.
- **Route enrichment**: reverse-DNS hostnames per hop (bounded,
  concurrent, `--no-resolve` to skip) and offline ASN annotation via
  `~/.netscope/ipasn.dat` (optional, license-clean, pure-Python
  longest-prefix-match) — wired through the new `run_route_analysis`
  use case.
- **`rich`-based terminal rendering** — panels, colored score levels,
  bounded detail columns; one shared renderer for CLI commands.
- **Project infrastructure**: GitHub Actions CI (3 OSes × Python
  3.11–3.13 + mypy job), ruff lint/format config, mypy config,
  `py.typed` marker, Makefile, CONTRIBUTING.md, SVG screenshots
  (regenerable via `scripts/generate_screenshots.py`).

### Changed

- `run_measurement_round` now loads the baseline from the container's
  repository when none is passed, and observes + saves successful
  readings after scoring/diagnosis (scoring/diagnosis remain read-only
  against the baseline — the round as a whole no longer is).
- The `route` command goes through `run_route_analysis` (app layer);
  route rendering gained Host/ASN/Loss columns.
- `--gateway` on `diagnose` defaults to `auto` instead of unset.
- Empty legacy packages (`diagnosis/`, `monitoring/`, `reporting/`,
  `routing/`, `infrastructure/`, `intelligence/`) removed — the real
  logic always lived in `core/`.
- Version bumped to `0.2.0`; README rewritten to describe the actual,
  current tool instead of the MVP skeleton.

### Fixed

- Stale docstring in `persistence/schema.py` claiming the `Service`
  model "does not exist yet".
- ICMP/HTTP probe library error strings no longer blow up table layout
  (bounded one-line detail).
- `RouteHop.asn` is now actually populated (as `AS<NNNN>`) when the
  optional offline ASN database is configured, and route churn remains
  immune to enrichment (signature covers hop addresses only).

## [0.1.0] — 2026-08-21

Initial MVP skeleton: six probe adapters behind a registry, baseline
scoring, evidence/hypothesis diagnosis, incident state machine, SQLite
measurement history, minimal `diagnose`/`route` CLI, and the full
architecture documentation set (ADRs 001–010).
