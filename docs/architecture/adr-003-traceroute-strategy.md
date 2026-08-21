# ADR-003 — Traceroute Strategy

**Status:** Accepted

## Context

Phase 1 research (§4) proposed combining `icmplib` with a system-binary
fallback for traceroute, explicitly rejecting `scapy` (GPL-2.0, would force
copyleft obligations onto anything that linked it directly) as the primary
approach. That research was written without verifying whether `icmplib`
itself already provides a traceroute capability. This ADR verifies that
directly against the installed package before deciding.

## Options evaluated

| Approach | Portability | Accuracy | Privileges | Dependencies | Maintenance | License | Performance | API quality |
|---|---|---|---|---|---|---|---|---|
| **`icmplib.traceroute()`** (verified present in the installed 3.0.4 package: `traceroute(address, count=2, interval=0.05, timeout=2, first_hop=1, max_hops=30, fast=False, ...)`, returns `list[Hop]` with `address`, `avg_rtt`, `packet_loss`, `distance`) | Cross-platform (same library already used for `ping`) | Good — ICMP Time-Exceeded based, standard technique | **Requires root/Administrator unconditionally** (confirmed from the library's own docstring — no unprivileged mode exists for traceroute, unlike `ping`) | None new — already a dependency | Active (same project as `ping`, already vetted) | LGPL-3.0-or-later — already cleared and attributed in `NOTICE` | Fine for a one-shot diagnostic tool; `fast=True` option available to reduce probe count per hop | Excellent — returns typed `Hop` objects directly, no text-parsing needed |
| **`scapy`-based raw traceroute** | Cross-platform (needs Npcap on Windows) | Good | Root/Administrator, plus raw packet crafting | New dependency | Active | **GPL-2.0** — already rejected in Phase 1 research and `NOTICE`; would need to run as a fully separate subprocess to avoid copyleft exposure to the rest of NetScope | Heavier than needed for this | Powerful but overkill; would require writing our own hop-parsing logic `icmplib` already provides |
| **System `traceroute`/`tracert` binary via `subprocess`** | Needs three different parsers (Linux `traceroute`, macOS `traceroute` — BSD variant, slightly different flags/output — and Windows `tracert`) | Good, but output format is not guaranteed stable across distros/OS versions | Varies: Windows `tracert` typically works for a standard user (uses ICMP directly via the OS); Linux/macOS `traceroute` binaries commonly need root or a `CAP_NET_RAW` capability/setuid bit depending on distro packaging | None new (calls an existing OS binary) | Not NetScope's to maintain, but output-format changes are a real, silent-breakage risk | N/A (not linked, just invoked) | Slower to parse, no structural guarantees | Poor — text scraping |
| **Raw sockets, implemented from scratch** | Needs per-OS raw-socket code | Depends entirely on our own correctness | Root/Administrator | None new | **We would own 100% of the maintenance burden** for something `icmplib` already solved | N/A | No inherent advantage over `icmplib` | We'd be re-implementing what `icmplib.traceroute` already is |

## Decision

**Primary: `icmplib.traceroute()`**, wrapped by `adapters/probes/traceroute.py`,
converting each `icmplib.Hop` into `core.models.Hop`. This is tier 1 of the
task's decision hierarchy (mature library exists and fits) and requires zero
new dependencies or licensing review — it inherits the LGPL-3.0 clearance
`icmplib` already has.

This **confirms, rather than contradicts**, Phase 1 research's direction
(icmplib-based, not scapy-based); the new information from this task is the
concrete verification that `icmplib` doesn't need a system-binary fallback to
work at all — it has traceroute built in — and the explicit confirmation that
it requires root/Administrator unconditionally, which Phase 1 research did not
know and this ADR records for the privilege-handling design (§ below).

**Optional secondary: system `traceroute`/`tracert` via `subprocess`**, kept as
a documented fallback adapter (also implementing `core.ports.Probe`) for
environments where raw ICMP sockets are unavailable to the process at all
(e.g. some sandboxed/managed environments, or a user who declines to grant
elevated privileges). `app`'s composition root selects which adapter to use;
`core` is unaware two exist.

## Privilege handling

Unlike `icmp_probe.ping()` (which the audit confirmed already falls back from
`privileged=True` to `privileged=False` on `SocketPermissionError`),
`icmplib.traceroute()` has no unprivileged mode. The adapter must therefore:

1. Attempt the traceroute.
2. On `icmplib.exceptions.SocketPermissionError`, return a `Measurement`-shaped
   failure with `error_type=PERMISSION_DENIED` (per architecture-overview.md
   §6) rather than crashing or silently retrying — there is nothing to retry
   into.
3. Let `app`/`ui` be responsible for telling the user *why* traceroute didn't
   run and what to do about it (e.g. "run with elevated privileges" or "switch
   to the system-traceroute fallback adapter") — `adapters` never prints
   anything itself, consistent with §3's dependency table.

## Consequences

- No new dependency, no new license to clear.
- Traceroute output is typed (`Hop` objects) from day one — no text-parsing
  layer needed for the primary path.
- The privilege requirement is a real UX consideration for the eventual UI
  (users will need to be told to run NetScope elevated, or fall back to the
  system-binary adapter) — noted here so it isn't rediscovered as a surprise
  during implementation.
