# Installation

## From PyPI (recommended)

```console
$ pip install netscope
```

Python 3.11–3.13 on Linux, macOS, and Windows. Some probes need extra system
capabilities:

- **ICMP ping / traceroute** use raw sockets when available. On Linux, either
  run as root, set `sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"`,
  or NetScope falls back to TCP-based evidence. On Windows the raw-socket path
  works out of the box. Without raw-socket permission the probes return a
  structured `PERMISSION_DENIED` error — reported honestly, never silently
  skipped.
- **DNS** probes need a resolver library (`dnspython`) — installed automatically
  as a dependency.

## Optional extras

```console
$ pip install "netscope[tui]"      # the live Textual dashboard
$ pip install "netscope[dev]"      # pytest, ruff, mypy, coverage — for contributors
```

## From source

```console
$ git clone https://github.com/Abolfazlrwm/netscope.git
$ cd netscope
$ pip install -e ".[dev]"
$ make test        # pytest + ruff + mypy — the same gates CI runs
```

## Where NetScope keeps its data

Everything lives under `~/.netscope/`:

| Path | Contents |
|---|---|
| `netscope.db` | SQLite: measurements, baselines (latency/loss *and* throughput) |
| `config.toml` | Optional configuration — see [Configuration](configuration.md) |

Nothing leaves your machine: no telemetry, no cloud account, no network calls
beyond the probes you ask for.
