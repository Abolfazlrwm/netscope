# ADR-018: The night shift -- the drift alarm speaks in exit codes

**Status:** Accepted (v0.20, 2026-11)
**Companion:** `adr-014` (the drift watch -- the verdict stream this alarm
listens to), `adr-002` (probe adapters -- why the alarm itself never probes)

## Context

The drift watch (v0.15) answers one question per evaluation: *is usual
getting worse?* — stable / degrading / improving / not enough data, per vital
and overall. But it only answers when a human asks: `netscope drift` prints a
table, exits 0 whatever the verdict, and goes home. The Prometheus door
(v0.17) made the log scrapeable from a cron line, but a scraper that reads
series is not an alarm — nothing *acts* on a degrading verdict at 3 a.m. The
roadmap reserved exactly one item after the storefront: "the night shift — a
scheduled watch loop that evaluates drift verdicts on every round and raises
threshold-crossing alerts with an exit code a scheduler or CI job can act on
(the cron line exists since v0.17; the alarm is the missing piece)."

Three constraints shaped the design:

1. **Local-first holds, by construction.** An alarm that emails, posts a
   webhook or opens a port would put NetScope on the network as a talker and
   add a notification stack — both against the privacy model and `adr-006`.
   The alarm must be something the *machine* already speaks.
2. **A verdict is not a line item; it is a transition.** A loop that re-asks
   the watch every N rounds does not want the verdict shouted at the top of
   its lungs forever — it wants the boundary crossings.
3. **"Not enough data" is not an alarm.** The watch's honesty rule (coverage
   is evidence) must survive contact with a scheduler: a cron line that
   pages on insufficient history would train its owner to ignore it.

## Decision

- **The exit code is the alarm.** `netscope drift <target>
  --fail-on-degrading` exits **1** when the overall verdict is degrading and
  **0** otherwise — stable, improving, *and* the honest "not enough data"
  (the same convention `history` sets for an empty log: a coverage finding
  is not a failure). `--json` honors the flag too. The scheduler stays the
  notifier — `netscope drift example.com --fail-on-degrading || mail ...` —
  so NetScope never opens a port, never posts a webhook, and the local-first
  promise costs nothing.
- **The monitor loop grows the watch.** `netscope monitor --drift-every N`
  re-asks the drift watch every Nth round for every target the round
  measured — a plain SQLite read behind `run_drift_analysis`, no new probes,
  no new threads. A `DriftAlarmSession` per target (app/monitoring.py, the
  same layer that owns the incident state machine) turns the verdict stream
  into transitions: **raised** on the first degrading verdict (including the
  first evaluation against an already-rotting history — silence there would
  hide the leak the loop exists to catch), dim **ongoing** while it stays,
  **cleared** when it stops. Exactly one honest line per evaluation, the
  same shape `MonitorSession` gives incident state.
- **`--fail-on-drift` exits 1** when an alarm is still active when
  monitoring stops, for a long-running supervisor that acts without parsing
  output; it requires `--drift-every` (an alarm about a watch that is never
  asked can never be active — that combination is a user error, rejected at
  the parser). Without the flag, the monitor's exit contract is unchanged:
  0 after a normal run, Ctrl+C included.
- **The alarm owns no thresholds.** `DriftAlarmSession` reads the overall
  verdict word core already produced; no second threshold table lives in
  app, and the window math stays exclusively in `core/drift.py` (`adr-014`).

## Reason / Alternatives / Trade-offs

- *Why not a webhook/notification door?* It would make NetScope a network
  talker (privacy), add a config surface and a dependency (or three), and
  every scheduler a user might already run can notify better than we can.
  The exit code composes with all of them; a webhook composes with none.
- *Why not evaluate drift every round by default?* The verdicts move on
  day-scale windows; asking more often burns reads to repeat itself. The
  default stays off (the monitor's behavior is unchanged until asked), and
  `--drift-every N` puts the cadence in the operator's hands.
- *Why transitions instead of state lines?* The incident machine set the
  pattern: opened/updated/closed is what a reader acts on. An alarm that
  repeats itself trains its reader to skim past it — the one line per
  boundary crossing is the line worth reading.
- Zero new dependencies — the eleventh consecutive release keeps `adr-006`
  true.
