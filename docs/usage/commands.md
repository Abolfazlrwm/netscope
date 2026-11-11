# Command reference

All commands accept `--verbose / -v` (INFO-level logging) *after* the
subcommand: `netscope diagnose -v`. Commands that produce a result also accept
`--json` — see [JSON output](json-output.md).

## `netscope diagnose`

Run one diagnostic round: gateway reachability, DNS resolution, and a public
HTTP(S) target, scored 0–100 against your personal baseline with an
evidence-based diagnosis.

| Flag | Meaning |
|---|---|
| `--gateway IP\|auto\|none` | gateway to probe: an IP, `auto` (detect from the OS — default), or `none` (skip the gateway check) |
| `--json` | machine-readable output |

A completed diagnosis always exits `0` — whether it found a problem or not is
not a CLI-level failure; read the diagnosis, don't scrape the exit code.

## `netscope check <target>`

Probe one target with any subset of the checks and report the result.

| Flag | Meaning |
|---|---|
| `--checks LIST` | comma-separated subset of `icmp,dns,tcp,tls,http` (default: all five) |
| `--json` | machine-readable output |

### Packet-loss confirmation

Every ICMP sweep NetScope runs — in `diagnose`, `check`, `service check` and
`monitor` — carries this rule (v0.9): a sweep that succeeds but reports partial
loss is a *suspicion*, not a finding, so it is re-swept twice and the rounds
merge into one measurement. The loss number you see is the window average, and
the detail column says which story it tells:

- `loss confirmed across 3 sweeps` — the loss reproduced in a later sweep; treat
  it as a property of the link right now.
- `loss transient across 3 sweeps` — every confirmation sweep came back clean;
  one stray packet dropped, nothing ongoing.

Clean sweeps and dead targets are never re-swept: confirmation costs nothing
until there is something to confirm.

## `netscope service`

Manage the **saved service list** — the curated set of targets you keep an
eye on, persisted locally (schema v3) and shared by the CLI and the TUI
services board. A saved service appears on the board *before* its first
check; removal is CLI-only (the TUI board has no destructive keys).

### `netscope service add <name> <host>`

Save (or update) a service. The `name` is the list's identity — re-saving
under the same name updates the entry in place, it never duplicates.

| Flag | Meaning |
|---|---|
| `--checks LIST` | comma-separated subset of `icmp,dns,tcp,tls,http` to enable (default: all five) |

### `netscope service list`

The saved list exactly as persisted, alphabetical by name — never merged
with log-derived targets (the TUI board does that join, visibly).

| Flag | Meaning |
|---|---|
| `--json` | machine-readable output (`report: "services"`) |

### `netscope service remove <name>`

Remove a saved service by exact name. Removing a name that isn't saved says
so — and lists what *is* saved — so a typo'd name is never a silent success.

### `netscope service check <name>`

Run a saved service's own curated checks now: the name, host and check set
come from the persisted entry, never re-typed. Same pipeline, score and
presentation as `netscope check`, and results persist to the same log.

| Flag | Meaning |
|---|---|
| `--json` | machine-readable output (`report: "service-check"`) |

## `netscope route <target>`

Traceroute to a target with hop RTT/loss and route-stability analysis.

Every run is stored locally (schema v1's `route_snapshots`/`route_hops`
tables, finally fed by v0.8), so the stability verdict compares against your
target's real history — not just the run that produced it:

- **first observation** — nothing stored yet; future runs compare against
  this path
- **unchanged across the last N runs — same path since <date>** — the stored
  path matches, hop for hop
- **changed vs <date> — hop K: <old> → <new>** — the path moved; which hop
  position and what it became (a hop that stopped answering reads as
  `→ no reply`)

| Flag | Meaning |
|---|---|
| `--no-resolve` | skip reverse-DNS hostname resolution for hops |
| `--json` | machine-readable output; `stability.last_change` carries the full hop diff (the human line caps at three hops) |

## `netscope history`

Show recent measurement rounds reconstructed from the local log, each scored
against the *current* baseline (the log alone cannot reconstruct the baseline
as it stood at each round's time — the report says so rather than pretending).

| Flag | Meaning |
|---|---|
| `--limit N` | maximum rounds to show (default: 20) |
| `--json` | machine-readable output |

## `netscope report`

Export a self-contained **evidence report** built from the persisted
measurement log: the window's rounds, per-target evidence tables, every
failure with its taxonomy, and the honesty notes. Markdown by default — ready
to attach to a ticket.

| Flag | Meaning |
|---|---|
| `--days N` | window length in days, ending now (default: 7) |
| `--target TARGET` | restrict the report to one target (default: every target in the window) |
| `--out FILE` | write the markdown to a UTF-8 file instead of stdout |
| `--json` | machine-readable payload instead of markdown |

Large windows are rendered honestly: per-target tables show the newest 40 rows
and *disclose* how many older rows were omitted — aggregates always cover the
full window.

## `netscope monitor`

Run continuous measurement rounds with incident detection: a problem must be
sustained for 3 consecutive rounds to open an incident, and a sustained-healthy
stretch to close one — single noisy samples never flap incident state.
Ctrl+C stops; a summary of open/closed incidents prints on exit.

| Flag | Meaning |
|---|---|
| `--interval S` | seconds between rounds (default: 30) |
| `--rounds N` | number of rounds before stopping (default: 0 = until Ctrl+C) |
| `--gateway IP\|auto\|none` | gateway to probe |

## `netscope speed`

Measure download throughput: a bounded-window download sampled at fixed
intervals, reported as **median** Mbps (robust against the TCP slow-start
ramp) with peak and bytes moved as evidence. Each run is compared against your
personal throughput baseline *before* it is folded in (compare-then-observe):
±15% of your usual reads as `as_usual`, beyond that
`slower_than_usual` / `faster_than_usual`. While the baseline is still young
(fewer than 3 runs) the comparison is simply absent — no invented verdict.

| Flag | Meaning |
|---|---|
| `--url URL` | download URL (default: the `bandwidth_url` config key) |
| `--seconds S` | measurement window (default: the `bandwidth_seconds` config key) |
| `--json` | machine-readable output |

A speed test never runs inside `diagnose` rounds — it is slow and data-hungry
by nature, and its result is throughput evidence, not a latency reading.

## `netscope tui`

Launch the live six-screen dashboard (v0.6). Requires the extra:
`pip install "netscope[tui]"`.

All screens share one round pipeline, one incident session and one local
log — switching screens never discards state, and a screen is never left
showing a round older than the shell's latest.

| Key | Screen | What it shows |
|---|---|---|
| `d` | Dashboard | Live 0–100 score, trend sparkline, per-probe results, diagnosis, incident transitions |
| `n` | Network | Every interface (name, type, state, addresses) and the detected gateway |
| `p` | Route | Traceroute to any host: per-hop hostnames, AS numbers, loss, RTT, churn verdict |
| `e` | Evidence | The full evidence table (observed vs expected vs deviation), ruled-out list, plain-language explanation |
| `h` | History | Recent rounds from the local log with recomputed scores; `enter` opens a round's measurement detail |
| `s` | Services | Status board: your saved services pinned (★) plus every measured target's latest result per check; type a host to check it, 'name host' to save |

Screen-local keys: `r` means "refresh" wherever you are — run a round on
the dashboard/evidence screen, re-trace on the route screen, reload from
the log on history/services, re-discover on network. `q` quits.

Notes:

- Round scores in the history screen are recomputed against the
  *current* personal baseline — the log cannot reconstruct the baseline
  as it stood at each round's time, and the screen says so rather than
  pretending otherwise (the same disclosure `history` and `report` make).
- The services board shows your saved services pinned at the top — a
  saved entry appears *before* its first check, with dashes for
  never-measured cells and "never" for last-seen — followed by every
  target the log holds. Saving from the board enables all five checks
  (the host is the last word you type); `netscope service add
  --checks` curates subsets.
- The route screen traces the configured DNS target on open; type
  another host and press enter to trace it.
