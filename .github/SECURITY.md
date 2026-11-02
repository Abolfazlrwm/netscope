# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a vulnerability

NetScope is an offline diagnostic CLI: it probes the network you point it at
and stores results under `~/.netscope/`. It ships no network service and no
telemetry, so the interesting attack surfaces are limited — but they exist:

- probe targets are parsed from the CLI/config (`http_probe` URL handling,
  DNS name handling, traceroute packet crafting);
- the SQLite stores under `~/.netscope/` (path handling, SQL);
- the ASN database loader (`adapters/lookups.py`) reads a third-party file.

Please report anything that lets a crafted target, config file, or lookup
database execute code, escape its directory, or exfiltrate data:

1. Open a **private vulnerability report** via GitHub's
   *Security → Report a vulnerability* on this repository. Do **not** open a
   public issue for security problems.
2. Include the exact command line, config, and a minimal reproduction.

You can expect an initial response within 72 hours and a fix or a mitigation
plan for confirmed issues in the next patch release.

## Scope notes

- NetScope intentionally performs active probing (ICMP, DNS, TCP, TLS, HTTP,
  traceroute). Only run it against networks and targets you are authorized to
  measure.
- Dependencies are declared as minimum versions in `pyproject.toml`; review the
  license notes in `NOTICE` (icmplib is LGPL-3.0 and dynamically imported).
