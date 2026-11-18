# ADR-016: The readings travel -- CSV and Prometheus, the log's plain-text exits

**Status:** Accepted (v0.17, 2026-11)
**Companion:** `adr-005-persistence-strategy.md` (the log these exports read),
`adr-006-dependency-policy.md` (zero new dependencies), `adr-014` (drift
watch -- the analysis this does NOT duplicate)

## Context

The post-TASK-046 roadmap reserved exactly one item: "export formats beyond
markdown/JSON/HTML." The evidence report already speaks three renderings --
markdown (`--out` or stdout, v0.5), JSON (`--json`, v0.5), HTML (`--html`,
v0.10) -- and the measurement log already answers `netscope history` with a
styled table and a `--json` payload. What none of those reach:

1. **The spreadsheet.** A log that accumulates for months is a dataset, and
   the tool people actually open datasets with eats CSV. The JSON payload is
   faithful but nested -- rounds containing measurements, targets containing
   capped row lists -- which is exactly the shape a pivot table does not
   want. A flat denormalized table with one row per reading is.
2. **The scraper.** A machine that watches NetScope's numbers over time has,
   since ~2014, one lingua franca: the Prometheus text exposition format,
   the dialect the node_exporter textfile collector reads. A cron line --
   `netscope report --prometheus /var/lib/node_exporter/netscope.prom` --
   turns the same local log the dashboards read into series any Prometheus
   can scrape, without NetScope ever opening a port.

## Decision

Both formats live in `app/reports.py`, beside every other wire rendering,
because app owns wire formats and ui only writes files -- the rule the
`--json` serializers established.

- **`netscope history --csv FILE`** (`history_rounds_csv`): one row per
  stored measurement, the owning round's timestamp and score denormalized
  onto every row (the log stores measurements flat; the round is a grouping
  key, not a separate table). `csv` stdlib module, `lineterminator="\n"`,
  explicit UTF-8 and `newline=""` -- the Windows-console rule every other
  exported file already obeys.
- **`netscope report --csv FILE`** (`evidence_report_csv`): one row per SHOWN
  evidence-table row -- the same `REPORT_ROWS_PER_TARGET` slice the markdown
  and HTML tables render, chronological within each target -- with the row's
  own persisted geo/asn enrichment unfolded onto scalar columns
  (`city/country/distance_km`, `asn/organization`). Never re-looked-up at
  export time: a report is what the log proves (`adr-011`/`adr-013`
  discipline). Failures ride along as rows; the truncation the human formats
  disclose in prose is disclosed by the command's confirmation line, because
  a CSV is data and carries no prose.
- **`netscope report --prometheus FILE`** (`evidence_report_prometheus`):
  gauge families in one fixed order -- report totals
  (`netscope_report_measurements/failures/targets/mean_score`), per-target
  aggregates (`netscope_target_measurements/failures/mean_latency_ms`), and
  the latest successful reading per check
  (`netscope_check_latency_ms/jitter_ms/packet_loss_pct` with
  `target`/`check` labels). Label values go through the spec's escaping;
  sample values are finite floats or the series is omitted entirely.

### Honesty rules, CSV edition

An absent reading is an EMPTY cell -- never a fabricated 0 and never the
human formats' `--`, which spreadsheets would parse as text or coerce.
Booleans are lowercase `true`/`false`. Timestamps keep the ISO-8601 form
persistence writes. The `csv` module owns quoting: an error message with
commas or quotes arrives in its cell intact.

### Honesty rules, Prometheus edition

An absent fact omits its SERIES, never emits a zero: no jitter readings in
the window means no `netscope_check_jitter_ms` family at all, HELP and TYPE
included. An unscored window omits `netscope_report_mean_score`. The report
totals are the one deliberate exception -- `measurements 0` over an empty
window is a proven fact, not a fabrication. "Latest successful reading per
check" is the value a scraper wants: the most recent PROVEN one, not a mean
over a window that ends whenever the report happened to be cut.

### One primary rendering per invocation

`--csv` and `--prometheus` are ignored under `--json`, exactly like
`--html` before them; the file exports coexist with each other and with the
stdout markdown (verified by a command test that writes all three at once).

## Consequences

- **Zero new dependencies** -- the ninth release in a row. The text
  exposition format is lines of text; we already speak lines of text. The
  `csv` module is stdlib.
- The man page and all three completion scripts gain the new flags through
  their existing sync mechanisms (the byte-for-byte man test, the
  every-option-in-every-script test) -- no new drift surface.
- Exports are window-bound and cap-bound like every rendering: a CSV of a
  huge window is the tables' slice, with the omission count on the
  confirmation line. Nobody exports a full raw log through the report;
  `netscope history --csv` is the full-log door.
- What stays out, deliberately: no HTTP listener (a daemon is a different
  program with a different failure surface), no `# UNIT` metadata lines
  (gauge semantics are declared; the unit lives in the metric name), no
  histogram/counter families (the log proves snapshots, not rates).
