# ADR-014: The drift watch -- windowed trend analysis over stored history

**Status:** Accepted (v0.15, 2026-11)
**Companion:** `adr-012-jitter-scoring.md` (the third vital sign),
`adr-011-geoip-strategy.md` (disclosure discipline), `adr-001-architecture-style.md`
(the hexagon this module stays inside)

## Context

Every judgment NetScope made through v0.14 compares a SINGLE moment
against the learned baseline: `core/scoring.py` weighs a sweep's
latency/loss/jitter sigma readings, `core/diagnosis.py` cites the same
sigmas as evidence, `core/baseline.py`'s throughput verdict compares
one speed test to the learned mean. All three lenses share one
structural blind spot, inherited from the baseline itself:

**The baseline is a trailing average of the very measurements it
judges.** A line that quietly rots -- an ISP's peering degrading week
over week, an aging router, a resolver that decays by a millisecond a
day -- never trips a threshold, because every new sweep is judged
against a baseline that rots with it. Yesterday's 31ms is "excellent"
once the baseline has learned 30ms; the 23ms of two weeks ago is not
part of any verdict anymore. Per-check scoring answers "is this moment
worse than usual?"; nothing answered "is USUAL getting worse?"

The post-TASK-046 roadmap named this gap "a drift watch over stored
history" and ordered it after the TUI geo/ownership surfacing; the
v0.15 cycle promoted it because it is the last lens the intelligence
layer genuinely lacked -- every other planned item is presentation or
export of facts the machine already computes.

## Decision

1. **The complementary lens needs no baseline.** `core/drift.py`
   compares the target's two most recent same-length windows of STORED
   history against each other: the recent window ([now - N days, now])
   and the same-length window before it ([now - 2N, now - N]), both
   ends inclusive (the measurement repository's own boundary
   semantics, applied in memory so the watch and the log cannot
   disagree). Nothing outside the two windows is consulted, however
   extreme -- a watch that lets a month-old reading dominate the
   comparison is not a watch.

2. **Four vitals, each with the statistic its data earns.** Latency,
   jitter and throughput use the MEDIAN of their windows (a window is
   a population of sweeps; a few congested sweeps must not move it --
   the same robustness the speed command's "median Mbps" convention
   chose). Packet loss uses the MEAN, the one deliberate asymmetry:
   loss readings are almost always 0.0 with spikes, so a median would
   read 0.0 for every window ever and the watch would be blind to
   exactly the leak it exists to catch; the mean is the honest carrier
   of "how often it actually dropped".

3. **Thresholds are borrowed, not invented twice.** The maturity rule
   is `MetricBaseline.deviation_sigma()`'s own 5 samples -- the same
   number scoring.py and diagnosis.py already reuse for their
   "too young to judge" problems; a window with fewer usable readings
   gets `insufficient_history`, not a weak opinion. The throughput
   band is `THROUGHPUT_VERDICT_BAND_PCT` (15%), the same percentage at
   which `compare_throughput` already calls a speed test slower/faster
   than usual -- a window-to-window move beyond that band is the same
   sentence at window scale, mirrored for direction (a shrinking
   download is degradation). Latency and jitter are the only new
   constants and they are paired rules, documented as such in
   `core/drift.py`: BOTH a relative and an absolute move must hold in
   the same direction (25% & 5ms; 30% & 1.5ms), because a purely
   relative rule cries wolf on small medians and a purely absolute one
   cries wolf on slow links. Loss is the one purely-absolute rule
   (2 percentage points of window mean) because relative change from a
   0.2% floor is meaningless.

4. **Degradation outranks improvement in the report-level verdict.**
   One degrading vital makes the whole report `degrading` even beside
   an improving one -- a watch that averaged a leaking latency against
   a healing jitter into "stable" would have missed its own point.
   `improving` only when nothing degrades; `stable` only when
   something judged and nothing moved; `insufficient_history` only
   when NO vital managed to judge anything at all.

5. **It lives in core, reads through app, renders in ui -- the same
   hexagon, one more spoke.** `core/drift.py` is pure (no I/O, no
   clock of its own: `now` is injected, the reproducibility rule
   `build_evidence_report`'s `until` set). `app/use_cases.py`'s
   `run_drift_analysis` makes ONE inclusive range query for the full
   two-window span and delegates; `ui/cli.py`'s `netscope drift`
   renders the table, the human verdict words ("no data" -- the
   machine word `insufficient_history` stays in the JSON payload) and
   the evidence sentences. The exit code is always 0 when the analysis
   ran: an insufficient verdict is an honest finding about coverage,
   not a failed command.

6. **Zero new dependencies.** `statistics.median`/`statistics.fmean`
   and the existing models -- the seventh release in a row with no new
   third-party package (`adr-006-dependency-policy.md`).

## Consequences

- The drift watch is a NEW lens, not a scoring change: it never reads
  the baseline and never mutates it, the same read-only discipline
  scoring.py documents. A sweep's score and the log's drift can
  legitimately disagree -- that disagreement IS the signal.
- Throughput drift only appears for targets the user speed-tests; the
  table renders those rows as "no data" instead of hiding them --
  coverage is evidence too (the disclosure rule adr-011 set).
- The `run_report_command` tests' fixed-date seeds aged out of their
  7-day window nine days after v0.14 shipped -- writing tests that pin
  a clock is now the documented pattern everywhere a window meets a
  seed date; the report command grew `until` pass-through for exactly
  this.
- Remaining roadmap: surfacing geo/ownership on the TUI
  dashboard/network screens, and export formats beyond
  markdown/JSON/HTML.
