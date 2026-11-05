"""
Download-bandwidth probe.

Uses `httpx` (BSD-3-Clause) as a library dependency.

WHAT IT MEASURES
----------------
Download throughput over a bounded time window: stream a URL's response
body, count bytes against a monotonic clock (time.perf_counter(), never
datetime -- wall clock is unsuitable for elapsed-duration measurement,
the same convention every other probe follows), and take a throughput
sample at a fixed interval. The headline number is the MEDIAN sample --
robust against the TCP slow-start ramp at connection start and against
one-off scheduler hiccups mid-stream -- with the peak sample and the
total bytes transferred kept alongside it as evidence.

WHY A SEPARATE PROBE, NOT A MODE OF THE HTTP PROBE
---------------------------------------------------
The HTTP probe (TASK-018) deliberately stops after the first body
chunk: its job is time-to-first-byte, and "never do more work than the
measurement requires" is its documented convention. Throughput is the
opposite requirement -- it needs the opposite behavior (keep reading,
count everything) -- so sharing a module would force one of the two
probes to violate its own convention. A separate netscope.probes
module keeps each convention intact, mirrors how every other probe
type (ICMP/DNS/TCP/TLS/traceroute) owns its module, and lets the two
fail independently: a slow CDN must not read as "your internet is
slow" when the latency probe can say otherwise.

WHAT THIS PROBE DOES NOT DO
----------------------------
It does not score, and it does not feed the experience score's
latency/packet-loss baselines: latency_ms is left None on purpose, so
a bandwidth run can never masquerade as a latency reading in
score_measurements() or observe_measurements(). Since v0.4, download
results have their OWN baseline (UserBaseline.throughput -- Welford
over Mbps, stored in its own table, compared percentage-wise) that the
speed use case learns and compares against; that segregation lives in
core/baseline and app/use_cases, never as a mislabeled field here.

The default download URL is configuration (app.config's
`bandwidth_url`), not a constant here -- the same rule that keeps
public_dns_target/public_cdn_url out of the probes. Whatever URL is
configured, this module only ever measures it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from statistics import median
from typing import Any

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement, utcnow

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


def _iter_download_chunks(url: str, timeout: float) -> Iterator[bytes]:
    """Yield the response body of `url` as raw chunks.

    The single seam where real network I/O happens, so tests can
    monkeypatch this one generator and drive measure() with synthetic
    chunk streams (the same "patch the boundary, test the logic"
    approach the adapter tests use). Raises httpx.HTTPStatusError for
    4xx/5xx responses so the caller's classification sees it as an
    ordinary httpx request error.
    """
    with httpx.Client(timeout=timeout, follow_redirects=True) as client, client.stream("GET", url) as response:
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        yield from response.iter_bytes()


def measure(
    url: str,
    *,
    duration_s: float = 6.0,
    sample_interval_s: float = 0.5,
    timeout: float = 10.0,
) -> RawMeasurement:
    """Measure download throughput against `url` for up to `duration_s`
    seconds, sampling every `sample_interval_s` seconds.

    The measurement ends at whichever comes first: the window elapsing
    or the body ending -- a fast, small endpoint is a legitimate result
    (fewer bytes, shorter duration), not an error.
    """
    # Convenience normalization, mirroring http_probe.fetch(): a bare
    # hostname gets https:// prepended. Explicit schemes pass through.
    if "://" not in url:
        url = f"https://{url}"
    if not _HTTPX_AVAILABLE:
        return RawMeasurement(
            probe_type=ProbeType.BANDWIDTH,
            target=url,
            success=False,
            error="httpx not installed",
            error_type=ProbeErrorType.PROBE_UNAVAILABLE,
        )
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s!r}")

    start = time.perf_counter()
    total_bytes = 0
    sample_bytes = 0
    samples: list[float] = []
    last_sample_at = start

    try:
        for chunk in _iter_download_chunks(url, timeout=timeout):
            now = time.perf_counter()
            size = len(chunk)
            total_bytes += size
            sample_bytes += size
            if now - last_sample_at >= sample_interval_s:
                samples.append(sample_bytes * 8 / (now - last_sample_at) / 1e6)
                last_sample_at = now
                sample_bytes = 0
            if now - start >= duration_s:
                break
        elapsed_s = time.perf_counter() - start
        # The tail of the last (partial) sampling window is real
        # evidence too -- drop it only when it is empty.
        if sample_bytes > 0:
            tail_s = max(elapsed_s - (last_sample_at - start), 1e-9)
            samples.append(sample_bytes * 8 / tail_s / 1e6)
    except httpx.HTTPStatusError as exc:
        # 4xx/5xx from the download endpoint itself -- an HTTP-layer
        # failure of this probe's own request, same bucket the HTTP
        # probe uses for its request-level failures.
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.BANDWIDTH,
            target=url,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.HTTP_FAILURE,
        )
    except httpx.TimeoutException as exc:
        # Covers ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout
        # -- all subclasses of httpx.TimeoutException. A stalled stream
        # is a timeout of the measurement, not a throughput reading.
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.BANDWIDTH,
            target=url,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.TIMEOUT,
        )
    except httpx.RequestError as exc:
        # ConnectError, ReadError, WriteError, ProtocolError, ProxyError
        # -- transport-level failures, kept in the same single
        # HTTP_FAILURE bucket the HTTP probe uses (same reasoning).
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.BANDWIDTH,
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
            probe_type=ProbeType.BANDWIDTH,
            target=url,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.UNKNOWN,
        )

    if samples:
        throughput_mbps = median(samples)
        peak_mbps = max(samples)
    else:
        # No full sample window elapsed (e.g. the body finished almost
        # instantly): fall back to one aggregate sample over the whole
        # run so a tiny-but-real download still reports a number.
        throughput_mbps = total_bytes * 8 / elapsed_s / 1e6 if elapsed_s > 0 else 0.0
        peak_mbps = throughput_mbps

    return RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target=url,
        timestamp=utcnow(),
        success=True,
        extra={
            "throughput_mbps": round(throughput_mbps, 2),
            "peak_mbps": round(peak_mbps, 2),
            "bytes_transferred": total_bytes,
            "duration_s": round(elapsed_s, 2),
            "sample_count": len(samples),
        },
    )


def summarize(measurement: RawMeasurement) -> dict[str, Any]:
    """A flat, JSON-ready view of a bandwidth measurement's evidence --
    the exact dict the `speed` command prints under --json. Reads only;
    never invents values for fields the measurement does not carry."""
    if not measurement.success:
        return {
            "target": measurement.target,
            "success": False,
            "error": measurement.error,
            "error_type": measurement.error_type.value if measurement.error_type else None,
        }
    summary = {
        "target": measurement.target,
        "success": True,
        "throughput_mbps": measurement.extra.get("throughput_mbps"),
        "peak_mbps": measurement.extra.get("peak_mbps"),
        "bytes_transferred": measurement.extra.get("bytes_transferred"),
        "duration_s": measurement.extra.get("duration_s"),
        "sample_count": measurement.extra.get("sample_count"),
    }
    # Baseline-relative context, present only when the speed use case
    # attached it (the learned baseline had an opinion this run). Keys
    # stay absent -- not null -- so a young baseline yields the exact
    # v0.3 shape.
    for key in ("baseline_mean_mbps", "baseline_samples", "deviation_pct", "verdict"):
        if key in measurement.extra:
            summary[key] = measurement.extra[key]
    return summary
