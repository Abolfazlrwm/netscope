# Changelog

All notable changes to NetScope are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
