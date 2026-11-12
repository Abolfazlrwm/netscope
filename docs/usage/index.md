# NetScope — Usage Documentation

NetScope turns raw network measurements into evidence-based answers: *what's
wrong, and where* — your Wi-Fi, your ISP, the DNS resolver, or the target
service itself. Every claim it makes carries the evidence that produced it, and
every metric it did not test is reported as **untested**, never silently as
healthy.

This section is the user-facing documentation. The engineering documents
(architecture overview, ADRs, module boundaries) live in
[`docs/architecture/`](../architecture/architecture-overview.md).

| Document | What it covers |
|---|---|
| [Installation](installation.md) | Installing NetScope from PyPI, from source, and the optional extras |
| [Quickstart](quickstart.md) | The first five commands to run |
| [Command reference](commands.md) | Every command, flag, and exit behavior |
| [Configuration](configuration.md) | `~/.netscope/config.toml` keys and the local database |
| [Geo location](geoip.md) | The optional GeoLite2 feature: setup, home configuration, distance, `extra.geo` keys (v0.12) |
| [JSON output](json-output.md) | The `--json` payloads for scripting |

The two ideas NetScope measures against:

- **Your personal baseline.** Every round and every speed test updates a local
  Welford baseline per target. The 0–100 experience score is relative to what
  *your* line usually does, not to a global table of thresholds. From v0.4,
  throughput has its own baseline: `netscope speed` judges each run against
  your usual Mbps on a ±15% band.
- **Evidence discipline.** A download reading never poses as latency evidence;
  untested checks never read as healthy; reports disclose what they omit.
