"""
HTTP(S) probe.

Uses `httpx` (BSD-3-Clause) as a library dependency. Measures time-to-first-byte
style latency, which is a good proxy for "does this specific service feel slow",
distinct from raw ICMP latency to the same host.
"""

from __future__ import annotations

import time

from netscope.core.models import ProbeType, RawMeasurement, utcnow

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


def fetch(url: str, timeout: float = 5.0) -> RawMeasurement:
    if not _HTTPX_AVAILABLE:
        return RawMeasurement(
            probe_type=ProbeType.HTTP,
            target=url,
            success=False,
            error="httpx not installed",
        )

    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.HTTP,
            target=url,
            timestamp=utcnow(),
            success=response.status_code < 400,
            latency_ms=elapsed_ms,
            extra={
                "status_code": response.status_code,
                "http_version": response.http_version,
                "final_url": str(response.url),
            },
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.HTTP,
            target=url,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
        )
