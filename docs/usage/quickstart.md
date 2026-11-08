# Quickstart

## 1. Diagnose your general connectivity

```console
$ netscope diagnose
```

One measurement round — gateway reachability, DNS resolution, and a public
HTTP(S) target — scored 0–100 against *your* baseline, with an evidence-based
diagnosis if something is wrong. Run it once now, and again whenever the
internet "feels slow": the comparison is the answer.

Every round is saved to the local log (`~/.netscope/netscope.db`).

## 2. Check one specific service

```console
$ netscope check api.example.com
$ netscope check netflix.com --checks icmp,tcp,http
```

Probes one target with any subset of the checks (`icmp,dns,tcp,tls,http`) —
the same engine `diagnose` uses, pointed wherever you care about.

## 3. Trace the route

```console
$ netscope route api.example.com
```

Traceroute with hop-by-hop RTT and loss, plus route-churn analysis when you
compare runs over time — so "the path changed" becomes evidence, not a
suspicion.

## 4. Measure the pipe

```console
$ netscope speed
```

A bounded-window download measuring median Mbps (robust against the TCP
slow-start ramp), peak Mbps, and bytes moved. From your third run on, each test
is judged against **your personal throughput baseline**:

```text
Download   78.4 Mbps   median of 7 samples
Peak       91.2 Mbps
Moved      24.6 MB
vs your usual 82.1 Mbps · −4.5% — as usual
```

Within ±15% of your usual reads as `as usual`; beyond that you get
`slower than usual` / `faster than usual`.

## 5. Look back — then export the evidence

```console
$ netscope history                 # recent rounds, scored
$ netscope report --days 7         # the week's evidence as one markdown document
$ netscope report --days 30 --out week.md
```

The report contains the rounds table, per-target evidence, every failure with
its error taxonomy, and the notes that keep the numbers honest — ready to
attach to an ISP ticket or an internal issue.

## Going further

- `netscope monitor` — continuous rounds with incident detection
  (Ctrl+C to stop)
- `netscope tui` — the live dashboard (`pip install "netscope[tui]"`)
- `netscope <command> --json` — machine-readable output for scripting
  (see [JSON output](json-output.md))
