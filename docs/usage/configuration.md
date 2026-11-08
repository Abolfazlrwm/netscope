# Configuration

NetScope is zero-required-configuration: without a config file everything uses
sensible defaults. Optional configuration lives at:

```text
~/.netscope/config.toml
```

Unknown keys are ignored rather than rejected — a typo never crashes a probe
run.

## Keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `gateway` | string | *(auto-detect)* | gateway to probe in `diagnose`/`monitor`: an IP, or leave unset for OS auto-detection |
| `public_dns_target` | string | `1.1.1.1` | the DNS resolver probed for upstream resolution health |
| `dns_lookup_domain` | string | `example.com` | the name resolved in diagnostic rounds |
| `public_cdn_url` | string | `https://www.cloudflare.com/` | the public HTTP(S) target of a diagnostic round |
| `bandwidth_url` | string | Cloudflare's speed endpoint | the download endpoint `netscope speed` measures |
| `bandwidth_seconds` | float | `6.0` | the speed measurement window in seconds |

### Example

```toml
gateway = "192.168.1.1"
public_cdn_url = "https://www.gstatic.com/generate_204"
bandwidth_url = "https://speed.example/down?bytes=104857600"
bandwidth_seconds = 10.0
```

## The local database

`~/.netscope/netscope.db` (SQLite) holds everything NetScope remembers:

| Table | Contents |
|---|---|
| `measurements` | one row per probe reading — the evidence log every command reads |
| `metric_baselines` | your personal latency/packet-loss baseline per target (Welford: count/mean/M²) |
| `throughput_baselines` | your personal throughput baseline per target (v0.4, its own store — a download reading never poses as latency) |

Delete the file and NetScope starts learning from scratch; nothing is cached
anywhere else. The schema version is tracked with `PRAGMA user_version`, and
migrations are additive — a v0.3 database opens unchanged under v0.5.
