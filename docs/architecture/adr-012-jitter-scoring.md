# ADR-012 — Jitter Scoring: The Third Vital Sign, Learned Not Thresholded

**Status:** Accepted (v0.13)

## Context

Every ICMP sweep NetScope has ever run carried a delay-variation
reading: icmplib computes the mean absolute difference between
consecutive RTTs, and `RawMeasurement.jitter_ms` has transported it
since the first release. The CLI table has shown the column for a long
time. What nothing did was *listen* — the reading was never observed
into a baseline, never weighed in the experience score, never cited as
diagnosis evidence, and absent from the evidence tables and the TUI
dashboard.

That is a real blind spot, and a familiar one: a fast, lossless line
that wobbles (bufferbloat bursts, congested Wi-Fi, flaky radio links)
*feels* worse than a steady one with identical mean latency and zero
loss — for VoIP, calls and games it is the difference between fine and
unusable. Every prior release treated delay variation as a curiosity;
the score's two pillars (latency, packet loss) could not see it.

The open question this ADR decides: **how jitter should change what
NetScope says about a connection** — static quality bands ("< 5 ms is
excellent"), a second percentage-based comparison, or the learned
baseline treatment.

## Decision

**Jitter joins the sigma-scored family, learned per target — never
thresholded.**

1. **Baseline-relative, like latency and loss.** A fixed band would
   repeat the exact mistake TASK-025 retired: 3 ms of variation is a
   bad evening on a 20 ms fibre line and a miracle on a 200 ms
   satellite link. Only this user's own history can tell those apart.
   `UserBaseline` gains a `jitter` store with the same Welford
   tracker, the same 5-sample maturity rule and the same 2.5σ anomaly
   anchor the other pillars use.

2. **Its own store and table — the throughput precedent.** A jitter
   number must never be mistakable for a latency reading, and SQLite
   cannot ALTER a CHECK constraint: so `jitter_baselines` (schema v4)
   mirrors `throughput_baselines` (schema v2) — a new, purely-additive
   table with the Welford-exact `count`/`mean`/`m2` triple. Every v3
   database upgrades in place; nothing is rebuilt, moved, or dropped.

3. **The reading is consumed as-is, never re-derived.** icmplib's
   per-sweep jitter (mean absolute successive RTT difference, the
   RFC 3550-style formulation) is what `RawMeasurement.jitter_ms`
   carries, and that is what gets observed. NetScope does not compute
   a second, competing jitter number from `extra["min_rtt_ms"]`/
   `max_rtt_ms` — one honest measurement per sweep, the same doctrine
   `core/loss.py` applies to loss confirmation rounds.

4. **Score: the third pillar, zero-confidence when absent.**
   `score_measurements` evaluates `baseline.jitter` per measurement
   next to latency and loss. DNS/TCP/HTTP probes never report jitter;
   those evaluations weigh nothing (the zero-confidence neutral rule
   latency already follows), so the pillar only speaks when a sweep
   actually reported a variation reading.

5. **Diagnosis: a `_jitter` twin for every latency citation.**
   `core/diagnosis.py` gains `evidence_from_jitter`, and the wiring
   adds it everywhere latency evidence was already wired: the
   connectivity round (`gateway_jitter`, `dns_jitter`,
   `destination_jitter`) and the check/service flows
   (`service_*_jitter`, which the `service_` prefix already
   categorizes as SERVICE_ISSUE). A wobble beyond the mature
   baseline's anomaly anchor is WARNING/CRITICAL evidence; an absent
   reading is tested-but-zero-confidence — never a fabricated healthy
   or bad claim.

6. **Visible wherever the evidence is.** The TUI dashboard's checks
   table gains a Jitter column; evidence-report rows carry `jitter_ms`
   in `--json`, and both the markdown and HTML per-target tables gain
   a `jitter_ms` column. Absent readings render as absent (`--`/`--`),
   not zero — the honesty doctrine every surface already follows.

## Consequences

- **Scores can move.** For users with mature baselines, a wobbly line
  now scores below a steady one with identical latency/loss figures.
  This is the point of the release, and it is a deliberate behavior
  change — the changelog says so in kind.
- **No new dependencies, no migration risk.** The feature consumes a
  reading icmplib already produced and a column the measurements table
  already had; the only schema change is one additive table.
- **Maturity applies.** A fresh target needs ~5 sweeps before jitter
  claims anything — the same "insufficient history is not healthy" rule
  every metric follows, so a new install sees no score change until a
  personal history exists.
- **Still one reading per family.** The confirmation-window merge
  (`core/loss.py`) already averages the rounds' jitter readings, so a
  merged loss window reports one jitter figure — consistent with how
  latency is merged there.
