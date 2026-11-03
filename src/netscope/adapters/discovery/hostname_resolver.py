"""
netscope.adapters.discovery.hostname_resolver

Reverse-DNS hostname resolution for traceroute hops -- best-effort,
bounded-time, off the hot path.

WHY THIS EXISTS
---------------
`netscope route` prints hop addresses; hostnames make routes humanly
readable ("one.hundredgige...-ix.lax1.att-gw.net" says far more than
"12.122.128.1"). The traceroute probe itself deliberately does NOT
resolve hostnames (icmplib doesn't, and resolution shouldn't slow or
complicate the measurement itself), so resolution is a separate,
optional enrichment the route-analysis use case applies when asked.

DESIGN
------
- ThreadPoolExecutor so the per-hop resolution happens concurrently:
  a 15-hop route resolves in the time of its slowest hop, not their
  sum.
- `socket.gethostbyaddr` inside a daemon-thread pool; the pool is
  shut down with a bounded wait (`timeout`), so one pathological
  resolver can only delay the command by the configured timeout, not
  hang it.
- Never raises: any failure per hop (NXDOMAIN, timeout, no reverse
  record) simply leaves that hop's hostname as None.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor


def _resolve_one(address: str) -> str | None:
    """Single reverse-DNS lookup. Returns the canonical hostname, or
    None if the address has none. Never raises."""
    try:
        hostname, _, _ = socket.gethostbyaddr(address)
        return hostname.rstrip(".") or None
    except (socket.herror, socket.gaierror, OSError, UnicodeError):
        return None


class HostnameResolver:
    """Concurrent, bounded-time reverse-DNS resolution for hop
    addresses. `timeout` bounds the *entire* batch wait, not per-hop
    DNS timeout (stdlib gethostbyaddr has no timeout parameter; the
    executor-level deadline is the practical bound)."""

    def __init__(self, timeout: float = 4.0, max_workers: int = 16):
        self._timeout = timeout
        self._max_workers = max_workers

    def resolve_all(self, addresses: list[str | None]) -> dict[str, str | None]:
        """Resolve every non-None address in `addresses` concurrently.

        Returns a mapping address -> hostname (or None where no
        hostname exists / resolution failed / the batch deadline
        passed). Deterministic output: every requested address gets a
        key.
        """
        unique = list(dict.fromkeys(a for a in addresses if a))
        if not unique:
            return {}

        results: dict[str, str | None] = dict.fromkeys(unique)
        try:
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                futures = {pool.submit(_resolve_one, addr): addr for addr in unique}
                for future, addr in futures.items():
                    try:
                        results[addr] = future.result(timeout=self._timeout)
                    except Exception:  # noqa: BLE001 -- enrichment must never propagate
                        results[addr] = None
        except Exception:  # noqa: BLE001 -- a dead pool must not kill a route command
            return dict.fromkeys(unique)
        return results
