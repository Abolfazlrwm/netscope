# JSON output

Every result-producing command accepts `--json` for machine-readable output:
one JSON document on stdout, valid as-is for `jq`/`python -m json.tool`. The
payloads share a canonical envelope:

```json
{ "tool": "netscope", "report": "<kind>", "...": "kind-specific fields" }
```

`RawMeasurement` fields (nested wherever measurements appear): `probe_type`,
`target`, `timestamp` (ISO-8601 with UTC offset), `success`, `latency_ms`,
`packet_loss_pct`, `jitter_ms`, `error`, `error_type` (one of the structured
`ProbeErrorType` values, or `null`), and `extra` (probe-specific, JSON-safe).

ICMP measurements whose sweep showed partial loss carry the confirmation window
(v0.9) in `extra`: `loss_rounds` (per-sweep loss percentages, in run order),
`loss_confirmed` (`true` when any sweep after the first also lost packets),
`loss_window_rounds` (sweeps in the window), with `packet_loss_pct` reporting
the window average. Sweeps that came back clean — or dead — carry none of
these keys.

When the optional GeoLite2 database is installed (v0.12 — see the
[geo setup guide](geoip.md)), measurements whose target resolved to an IP
carry `extra.geo`: `ip`, `country`, `country_code`, `city`, `latitude`,
`longitude`, `distance_km` (null unless your home is configured too), `db`,
`db_age_days`, and `stale_db` (true once the database passes its 30-day
refresh rhythm). Unknown addresses and absent databases leave `extra.geo`
out entirely — no geography is never fabricated.

## `diagnose --json` — `report: "diagnose"`

```json
{
  "tool": "netscope",
  "report": "diagnose",
  "gateway": "192.168.1.1",
  "experience": { "score": 82.0, "level": "good", "timestamp": "…" },
  "diagnosis": { "classification": "…", "confidence": 0.0, "timestamp": "…",
                 "evidence": [ { "metric": "…", "tested": true, "observed_value": 0.0,
                                 "expected_value": 0.0, "deviation": 0.0,
                                 "severity": "info", "confidence": 0.0 } ],
                 "ruled_out": [ ] },
  "measurements": [ { "…": "RawMeasurement" } ]
}
```

`diagnosis` is `null` for the confirmed-healthy convention — the JSON consumer
sees the same three states the domain has: healthy (`null`), insufficient
evidence, or a confirmed classification.

## `check --json` / `route --json`

`check` reports the target's measurements and experience; `route` adds
`hops` — each `{ "ttl", "address", "hostname", "asn", "organization",
"country", "avg_rtt_ms", "packet_loss_pct" }`, with unresolved hops as honest
`null`s (the `*` glyph is a presentation concern, never in the payload).

## `service list --json` — `report: "services"`

```json
{ "tool": "netscope", "report": "services",
  "services": [ { "name": "My VPN", "host": "vpn.example.net",
                  "checks": ["icmp", "http"] } ] }
```

The saved list exactly as persisted, alphabetical by name; `checks` follows
the CLI's fixed `icmp,dns,tcp,tls,http` order, not storage order.

## `service check --json` — `report: "service-check"`

The `check` payload plus one field: `service` (the curated name). `target`
is the saved service's host, and the check set comes from the persisted
entry — a saved service is checked exactly as it was defined.

## `history --json` — `report: "history"` — `report: "history"`

```json
{ "tool": "netscope", "report": "history",
  "rounds": [ { "timestamp": "…", "experience": { "…": "…" } | null,
                "measurements": [ { "…": "RawMeasurement" } ] } ] }
```

## `speed --json` — v0.4 verdict fields

The speed payload carries the measurement's `extra`, plus — once the
throughput baseline has ≥ 3 observations — the baseline-relative verdict:

```json
{ "baseline_mean_mbps": 82.1, "baseline_samples": 7,
  "deviation_pct": -4.5, "verdict": "as_usual" }
```

The keys are **absent entirely** while the baseline is too young to have an
opinion — the v0.3 shape is preserved for scripts that don't opt in.
Verdicts are percentage-based: within ±15% of your usual reads as
`as_usual`.

## `report --json` — `report: "evidence"`

```json
{
  "tool": "netscope", "report": "evidence",
  "window": { "since": "…", "until": "…" },
  "scope": null,
  "totals": { "targets": 2, "rounds": 12, "measurements": 47,
              "failures": 4, "mean_score": 82.3 },
  "rounds":   [ { "timestamp": "…", "experience": { "…": "…" } | null,
                  "measurements": [ { "…": "RawMeasurement" } ] } ],
  "targets":  [ { "target": "…", "probe_types": ["icmp"], "measurements": 0,
                  "failures": 0, "mean_latency_ms": 0.0,
                  "verdict_counts": { "as_usual": 4 },
                  "truncated_rows": 0, "rows": [ { "…": "row" } ] } ],
  "failures": [ { "…": "row" } ]
}
```

`scope` is the `--target` filter (or `null` for all targets). Per-target `rows`
carry the newest 40 measurements; `truncated_rows` discloses what was omitted,
while all aggregates (`measurements`, `failures`, `mean_latency_ms`,
`verdict_counts`) always cover the full window.
