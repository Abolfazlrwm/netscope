"""
HTTP(S) probe.

Uses `httpx` (BSD-3-Clause) as a library dependency.

TASK-018 TTFB SEMANTICS FIX
------------------------------
The implementation_audit.md flagged a mismatch here: this module's
docstring claimed to measure "time-to-first-byte style latency," but
the implementation called `client.get(url)`, which downloads the
COMPLETE response body before returning -- for anything but a
zero-byte response, that measures full-download time, not first-byte
time, and materially overstates latency for larger responses.

This is fixed by streaming the response (`client.stream("GET", url)`)
and stopping the timer as soon as the first body chunk is actually
available from `response.iter_bytes()`, rather than after the full
body has been read. `latency_ms` on a successful measurement now
genuinely means "time to first byte of the response body," measured
with a monotonic clock (time.perf_counter(), never datetime -- wall
clock is unsuitable for elapsed-duration measurement). The response is
explicitly closed immediately after the first chunk is observed -- this
probe's responsibility ends there, it does not download the rest of
the body, matching the ICMP/TCP/TLS probes' shared convention of never
doing more work than the measurement requires.
"""

from __future__ import annotations

import time

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement, utcnow

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
            error_type=ProbeErrorType.PROBE_UNAVAILABLE,
        )

    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                # Read only the first available chunk, then stop --
                # this is the actual "first byte" moment. response
                # headers/status_code are already available at this
                # point (httpx reads them before the `with` block body
                # runs); iterating is what reaches into the body itself.
                for _ in response.iter_bytes():
                    break
                ttfb_ms = (time.perf_counter() - start) * 1000
                # Explicitly done with the body now -- do not download
                # the rest, matching this probe's TTFB-only responsibility.
                response.close()

        return RawMeasurement(
            probe_type=ProbeType.HTTP,
            target=url,
            timestamp=utcnow(),
            success=response.status_code < 400,
            latency_ms=ttfb_ms,
            extra={
                "status_code": response.status_code,
                "http_version": response.http_version,
                "final_url": str(response.url),
            },
        )
    except httpx.TimeoutException as exc:
        # Covers ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout
        # -- all subclasses of httpx.TimeoutException.
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.HTTP,
            target=url,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.TIMEOUT,
        )
    except httpx.RequestError as exc:
        # Covers httpx's other transport/request-level failures
        # (ConnectError, ReadError, WriteError, ProtocolError,
        # ProxyError, etc.) -- genuine HTTP-layer problems distinct
        # from a timeout, but not cleanly further sub-classifiable
        # through httpx's own exception types without inspecting
        # exception-chaining internals, which this task's instructions
        # explicitly discourage relying on. Kept in one HTTP_FAILURE
        # bucket rather than guessing at finer categories.
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.HTTP,
            target=url,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.HTTP_FAILURE,
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
            error_type=ProbeErrorType.UNKNOWN,
        )
