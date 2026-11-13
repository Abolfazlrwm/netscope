# Changelog

All notable changes to NetScope are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.13.0] — 2026-11-13

The "steady hands" release: delay variation has ridden along on every
ICMP sweep since the first one -- icmplib reports it, the CLI table has
shown the column for ages -- but nothing listened. Nothing learned it,
nothing scored it, nothing diagnosed it: a fast, lossless line that
wobbles scored exactly like a steady one. v0.13 makes jitter the third
vital sign, with zero new dependencies and a purely-additive schema
migration (decision record: `docs/architecture/adr-012-jitter-scoring.md`).

### Added

- **Jitter joins the learned baseline** (`core/baseline.py`): a
  `jitter` store on `UserBaseline` with `observe_jitter()`, the same
  Welford treatment latency and packet loss get, deliberately in its
  own store -- the throughput precedent: a jitter number must never
  pose as a latency reading. Persisted Welford-exact in a new
  `jitter_baselines` table (schema v4, additive -- every v3 database
  upgrades in place, nothing rebuilt or moved).
- **The experience score weighs it** (`core/scoring.py`): the third
  baseline-relative pillar, next to latency and loss. 22 ms of
  variation is ordinary on a wobbly line and extraordinary on a steady
  one, and the score now says so. Probe types that never report
  jitter weigh nothing (zero confidence, neutral) -- the same rule
  latency already follows when a probe has no latency reading.
- **Diagnosable** (`core/diagnosis.py` + app wiring):
  `evidence_from_jitter` mirrors the latency/loss evidence ladder,
  and every diagnosis that cites latency now cites a `_jitter` twin --
  the connectivity round (`gateway_jitter`/`dns_jitter`/
  `destination_jitter`) and the check/service flows
  (`service_*_jitter`). A wobble beyond the mature baseline's anomaly
  anchor is WARNING/CRITICAL evidence; a check type without a reading
  is tested-but-zero-confidence, never a fabricated claim.
- **Visible everywhere the evidence is**: the TUI dashboard's checks
  table gains a Jitter column; evidence-report rows carry `jitter_ms`
  in `--json`, and the markdown and HTML per-target tables gain a
  `jitter_ms` column (absent readings stay absent, never zero).

### Notes

- 890 tests passing (21 new across baseline/scoring/diagnosis/
  persistence/schema/reports/TUI), ruff clean, mypy clean. A fresh
  target needs ~5 sweeps before jitter claims anything -- the same
  maturity rule every learned metric follows.
- Scores can move for users with mature baselines: a wobbly line now
  scores below a steady one with identical latency and loss. That is
  the point of the release, not a regression.

## [0.12.0] — 2026-11-12

The "where in the world" release, and the last promise of the route
model kept: TASK-023's distance estimation lands with **zero new
dependencies** -- a hand-written MaxMind DB reader (~250 lines of
stdlib against the public file-format spec), not the `geoip2`/
`maxminddb` libraries the roadmap had provisionally anticipated. The
decision (adr-011) repeats the pyasn lesson on purpose: the reader
compiles nothing, installs everywhere, and stays fully offline-testable
against a spec-exact synthetic fixture writer -- GeoLite2 data itself
is never shipped, fetched, or committed.

### Added

- **`GeoLookup` port** (`core/ports.py`) with `Place`/`GeoInfo` domain
  types, the haversine great-circle distance (`core/geo.py`, IUGG mean
  radius), and the staleness policy: a database older than 30 days --
  the GeoLite2 EULA's monthly refresh rhythm -- stamps `stale_db` and
  surfaces a visible "refresh recommended" notice.
- **Dependency-free mmdb reader** (`adapters/geoip.py`): search tree at
  record sizes 24/28/32, all data types including extended types and
  all four pointer sizes, IPv4-in-IPv6 via the reference readers'
  96-zero-bit entry walk, and the trailing metadata map. Conformance is
  pinned two ways offline: `tests/mmdb_fixture.py` writes real-format
  synthetic databases, and -- when the official `maxminddb` reference
  library happens to be installed locally -- crosscheck tests compare
  both readers' answers on the same bytes (skipped in CI; never a
  dependency).
- **Geo enrichment in every read path**: `check`/`service check`/
  `diagnose` show place, distance and db age in the measurement detail
  and in `--json` (`extra.geo`, flat JSON scalars -- stable keys from
  v0.12); `netscope route` fills each hop's **Country** column (the
  field `RouteHop.country` reserved since TASK-020); `netscope report`
  gains a **Geography** section in both markdown and `--html`, carrying
  the newest geo-aware measurement's recorded facts, never a re-lookup.
- **Home location, never guessed**: config gains `geo_db_path`,
  `home_ip`, `home_latitude`, `home_longitude`. Distance is computed
  only when both sides of the question exist -- explicit coordinates
  win over a `home_ip` lookup in the same database; with neither, the
  target's place still renders and the distance stays absent.
- **EULA compliance by construction** (`NOTICE` now carries the actual
  attribution): the database is always the user's own download at
  `~/.netscope/GeoLite2-City.mmdb` (or `geo_db_path`); a missing or
  corrupt file degrades to no-geo, the same honest-optional posture as
  `ipasn.dat`. Setup is documented end to end in `docs/usage/geoip.md`.

### Notes

- The reader was validated during development against MaxMind's own
  published test databases (GeoLite2-City-Test, decoder DB, IPv4/IPv6
  record-size sets) -- answers match the official library byte for
  byte; those files are used nowhere in the committed suite.
- 869 tests passing (32 format/adapter + 27 wiring/UI), ruff clean,
  mypy clean.

## [0.11.0] — 2026-11-12

The "meet the machine's expectations" release: the diagnostics have been
evidence-grade since v0.5; the shell-level plumbing around them now is
too. The three things every serious CLI ships -- a `--version` flag, tab
completion, and a man page -- added without a single new dependency.

### Added

- **`netscope --version`**: reads package metadata (never a hardcoded
  copy), exits 0 before any subcommand is parsed; an uninstalled raw
  checkout honestly reports `unknown` rather than guessing a number.
- **Shell completions** (`netscope completions bash|zsh|fish`): three
  hand-written scripts printed verbatim to stdout, install paths included
  as comments. They cover every command (including the `service`
  subcommands), every per-command flag, and the five `--checks` values --
  bash completes the last segment of a comma-separated list, so
  `icmp,d<TAB>` works. The command short-circuits before any config read
  or container build, and prints with plain `print()`, never rich -- a
  single ANSI escape would corrupt a sourced script.
- **A man page that cannot drift** (`docs/netscope.1`): rendered from the
  live argparse parser by `scripts/gen_man.py` (`make man`) -- every
  command section, option, positional placeholder, the exit codes, and
  the `~/.netscope` files. Option dashes are roff-escaped (`\-\-json`)
  everywhere, prose included, and the committed page must equal a fresh
  render byte for byte.

### Notes

- The completion scripts are static on purpose: a completion that ran
  `netscope` on every TAB press would be slower than the tool it
  completes. The trade-off is drift, so the tests pin every script to
  the real parser surface -- a future command or flag without a
  completion fails the build.
- Regenerating the man page is part of touching the CLI: `make man`,
  then commit, or the sync test fails. The generated page is
  deterministic (no auto-dates), so two renders never disagree.
- 787 tests passing -- the new surface carries its own offline suite
  (version fallback, script verbatim-ness, ANSI-free output, roff
  escaping, sync).

## [0.10.0] — 2026-11-12

The "hand it to someone who doesn't read terminals" release: the evidence
NetScope collects was locked inside a terminal and a markdown blob. v0.10
gives both a presentation layer -- a sparkline where a column of numbers
used to be, and an HTML report a manager, a customer or an ISP ticket can
actually open -- without adding a single dependency, script or network
fetch. The facts never move: both renderings claim exactly what the
markdown claimed, caps and disclosures included.

### Added

- **Score trend sparkline in `netscope history`** (`ui.render.sparkline`):
  one line of Unicode blocks under the rounds table -- `▁▂▃▄▅▆▇█` -- with
  the series' low and high. Rules it lives by: `None` scores (rounds with
  nothing scorable) are skipped, not flattened to zero; fewer than two
  scorable rounds is not a trend, so nothing prints; and the series
  normalizes to its own min..max, because a sparkline answers "how did it
  move", not "how bad is it".
- **Standalone HTML report** (`evidence_report_html`,
  `netscope report --html FILE`): the full evidence document as one dark,
  self-contained page -- a donut gauge for the mean score, an SVG sparkline
  of the round scores, a per-target latency sparkline, evidence and
  failure tables, and the markdown's honesty notes verbatim. No scripts,
  no external assets, no network calls; explicit UTF-8 (the report carries
  arrows and middle dots, and Windows consoles default to legacy code
  pages). User-controlled text -- targets, error strings -- is HTML-escaped
  on the way in: a hostile hostname renders as text, never as markup.
- **`--html` and `--out` compose**: one invocation can emit both the
  markdown and the HTML artifact; `--json` still short-circuits, as
  documented.

### Notes

- One facts, two fonts: the HTML renderer shares the markdown's constants
  (`REPORT_MAX_ROUNDS`, `REPORT_ROWS_PER_TARGET`, `REPORT_MAX_FAILURES`),
  its omission disclosures ("… N older row(s) omitted"), and its absent
  metric renders as `--`, never zero -- the two documents cannot disagree.
- Self-containment is tested, not promised: the suite asserts the page
  carries no `<script>`, no `<link>`, no `<img>`, and no `src=`/`href=`
  pointing anywhere.
- The sparkline and the SVG trend are presentation-only: no new
  dependency, no charting library -- eight Unicode blocks and two SVG
  primitives do all of it.

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
