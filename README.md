# NetScope

**Internet Experience Intelligence & Network Diagnostics Platform**

NetScope goes from raw network *measurement* to *understanding*: it doesn't
just show you a latency number, it tells you whether the problem is your
Wi-Fi, your ISP, or the destination service — with evidence.

This is an MVP skeleton built per the research/architecture phase
(`NetScope-Research-Phase1.md`). It proves the architecture end-to-end:

```
probes (icmp/dns/http) -> intelligence (baseline + experience score)
                        -> diagnosis (evidence-based likely cause)
                        -> explanation (human-readable)
                        -> persistence (local SQLite, privacy-first)
```

## What's implemented in this MVP

- `core/models.py` — data models (`RawMeasurement`, `RouteSnapshot`, `ExperienceEvent`, `Incident`)
- `probes/` — ICMP (via `icmplib`), DNS (via `dnspython`), HTTP (via `httpx`)
- `intelligence/baseline.py` — personal, statistical baseline learning (not a fixed threshold)
- `intelligence/experience_score.py` — combines multiple probes into one 0-100 score
- `diagnosis/engine.py` — rule-based evidence chain: local vs. ISP vs. destination-specific
- `explanation/explainer.py` — human-readable output, kept separate from diagnosis logic
- `persistence/sqlite_store.py` — 100% local-first storage, no data leaves your machine
- `ui/cli.py` — a minimal runnable CLI tying the whole pipeline together

## Not yet implemented (see `ROADMAP.md` / research doc)

- Traceroute / Route Intelligence (`routing/`)
- Incident detection across sustained time windows (`monitoring/`)
- Evidence report export (`reporting/`)
- ASN / GeoIP lookups (`infrastructure/`)
- Textual-based TUI (`ui/tui/`)

## Install

```bash
pip install -e .
```

## Run

```bash
netscope --gateway 192.168.1.1   # optional: your router's IP, to localize issues
```

## License

NetScope's own code is MIT-licensed (see `LICENSE`). It depends on
permissively-licensed third-party libraries — see `NOTICE` for details,
especially the LGPL-3.0 attribution for `icmplib`.
