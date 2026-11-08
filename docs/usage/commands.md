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

## `netscope route <target>`

Traceroute to a target with hop RTT/loss and route-stability analysis.

| Flag | Meaning |
|---|---|
| `--no-resolve` | skip reverse-DNS hostname resolution for hops |

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

Launch the live Textual dashboard. Requires the extra:
`pip install "netscope[tui]"`.
