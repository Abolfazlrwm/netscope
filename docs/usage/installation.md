# Installation

## From source (the supported path today — not yet on PyPI)

```console
$ git clone https://github.com/Abolfazlrwm/netscope.git
$ cd netscope
$ pip install -e ".[tui]"
$ netscope --version
```

> **Try it without installing** — [open the live sample evidence report](https://abolfazlrwm.github.io/netscope/), a real `netscope report --html` artifact generated on a locked-down VM, before you clone anything.

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
$ pip install -e ".[tui,dev]"      # the Textual dashboard + pytest, ruff, mypy
```

## From PyPI (once published — `pip install netscope` is a 404 today)

NetScope is not on PyPI yet (owner setup, tracked in the roadmap's Phase L).
Until that door opens, the source path above is the only supported install —
this section will change the day the package lands.

## For contributors

```console
$ pip install -e ".[tui,dev]"
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
