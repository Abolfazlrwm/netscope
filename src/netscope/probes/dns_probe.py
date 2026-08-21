"""
DNS probe.

Uses `dnspython` (ISC License) as a library dependency.
"""

from __future__ import annotations

import time

from netscope.core.models import ProbeType, RawMeasurement, utcnow

try:
    import dns.resolver
    _DNSPYTHON_AVAILABLE = True
except ImportError:
    _DNSPYTHON_AVAILABLE = False


def resolve(
    hostname: str,
    record_type: str = "A",
    resolver_ip: str | None = None,
    timeout: float = 2.0,
) -> RawMeasurement:
    """Resolve `hostname` and measure resolution latency.

    If `resolver_ip` is given, that DNS server is queried directly
    (useful for comparing e.g. the ISP resolver vs. a public resolver
    like 1.1.1.1 to localize a problem).
    """
    if not _DNSPYTHON_AVAILABLE:
        return RawMeasurement(
            probe_type=ProbeType.DNS,
            target=hostname,
            success=False,
            error="dnspython not installed",
        )

    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    if resolver_ip:
        resolver.nameservers = [resolver_ip]

    start = time.perf_counter()
    try:
        answer = resolver.resolve(hostname, record_type)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.DNS,
            target=hostname,
            timestamp=utcnow(),
            success=True,
            latency_ms=elapsed_ms,
            extra={
                "resolver": resolver_ip or "system default",
                "answers": [r.to_text() for r in answer],
            },
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.DNS,
            target=hostname,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            extra={"resolver": resolver_ip or "system default"},
        )
