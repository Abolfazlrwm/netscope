"""
Tests for netscope.probes.bandwidth_probe.

Fully offline: the single network seam (_iter_download_chunks) is
monkeypatched with synthetic chunk streams -- the same "patch the
boundary, test the logic" approach the adapter tests use. No real
HTTP traffic ever happens; httpx exception types are raised by the
fakes directly so the classification paths are exercised without a
server.
"""

from __future__ import annotations

import time

import httpx
import pytest

from netscope.core.models import ProbeErrorType, ProbeType
from netscope.probes import bandwidth_probe


def _fake_stream(chunks: list[bytes], delay_s: float = 0.0):
    """Build a replacement for _iter_download_chunks that yields
    `chunks` (optionally sleeping between them so the sampler sees
    real elapsed time), counting nothing itself."""

    def stream(url: str, timeout: float):
        for chunk in chunks:
            if delay_s:
                time.sleep(delay_s)
            yield chunk

    return stream


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_measure_reports_success_with_evidence(monkeypatch):
    chunks = [b"x" * 65536 for _ in range(10)]
    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", _fake_stream(chunks, delay_s=0.02))

    result = bandwidth_probe.measure("https://speed.example/down", duration_s=0.5, sample_interval_s=0.05)

    assert result.success is True
    assert result.probe_type is ProbeType.BANDWIDTH
    assert result.target == "https://speed.example/down"
    assert result.extra["bytes_transferred"] == 10 * 65536
    assert result.extra["throughput_mbps"] > 0
    assert result.extra["peak_mbps"] >= result.extra["throughput_mbps"]
    assert result.extra["sample_count"] >= 1
    assert result.extra["duration_s"] < 2.0  # the window is respected (+ slack for slow CI)
    # A bandwidth measurement must never pose as a latency reading --
    # that is what keeps it out of the latency baseline and the score.
    assert result.latency_ms is None


def test_measure_falls_back_to_one_aggregate_sample_when_body_ends_first(monkeypatch):
    """A small endpoint whose body finishes long before the window is a
    legitimate result: bytes over actual elapsed time, not an error."""
    chunks = [b"y" * 100_000 for _ in range(3)]
    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", _fake_stream(chunks))

    result = bandwidth_probe.measure("https://speed.example/down", duration_s=30.0)

    assert result.success is True
    assert result.extra["bytes_transferred"] == 300_000
    assert result.extra["throughput_mbps"] > 0
    assert result.extra["duration_s"] < 30.0


def test_measure_prepends_https_to_bare_hostnames(monkeypatch):
    captured: dict = {}

    def stream(url: str, timeout: float):
        captured["url"] = url
        yield b"z" * 10

    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", stream)
    result = bandwidth_probe.measure("speed.example/down", duration_s=0.1)
    assert captured["url"] == "https://speed.example/down"
    assert result.target == "https://speed.example/down"


def test_measure_rejects_non_positive_windows():
    with pytest.raises(ValueError):
        bandwidth_probe.measure("https://speed.example/down", duration_s=0)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def test_measure_classifies_timeout(monkeypatch):
    def stream(url: str, timeout: float):
        raise httpx.ReadTimeout("timed out")
        yield b""  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", stream)
    result = bandwidth_probe.measure("https://speed.example/down")
    assert result.success is False
    assert result.error_type is ProbeErrorType.TIMEOUT


def test_measure_classifies_connect_error_as_http_failure(monkeypatch):
    def stream(url: str, timeout: float):
        raise httpx.ConnectError("connection refused")
        yield b""  # pragma: no cover

    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", stream)
    result = bandwidth_probe.measure("https://speed.example/down")
    assert result.success is False
    assert result.error_type is ProbeErrorType.HTTP_FAILURE


def test_measure_classifies_http_status_error_as_http_failure(monkeypatch):
    request = httpx.Request("GET", "https://speed.example/down")
    response = httpx.Response(404, request=request)

    def stream(url: str, timeout: float):
        raise httpx.HTTPStatusError("HTTP 404", request=request, response=response)
        yield b""  # pragma: no cover

    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", stream)
    result = bandwidth_probe.measure("https://speed.example/down")
    assert result.success is False
    assert result.error_type is ProbeErrorType.HTTP_FAILURE
    assert "404" in (result.error or "")


def test_measure_classifies_unexpected_errors_as_unknown(monkeypatch):
    def stream(url: str, timeout: float):
        raise RuntimeError("boom")
        yield b""  # pragma: no cover

    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", stream)
    result = bandwidth_probe.measure("https://speed.example/down")
    assert result.success is False
    assert result.error_type is ProbeErrorType.UNKNOWN


def test_measure_reports_probe_unavailable_without_httpx(monkeypatch):
    monkeypatch.setattr(bandwidth_probe, "_HTTPX_AVAILABLE", False)
    result = bandwidth_probe.measure("https://speed.example/down")
    assert result.success is False
    assert result.error_type is ProbeErrorType.PROBE_UNAVAILABLE


# ---------------------------------------------------------------------------
# summarize() -- the --json view
# ---------------------------------------------------------------------------


def test_summarize_flat_json_view_for_success(monkeypatch):
    chunks = [b"q" * 50_000 for _ in range(4)]
    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", _fake_stream(chunks))
    result = bandwidth_probe.measure("https://speed.example/down", duration_s=0.2)

    view = bandwidth_probe.summarize(result)
    assert view["success"] is True
    assert view["target"] == "https://speed.example/down"
    assert view["throughput_mbps"] > 0
    assert view["bytes_transferred"] == 200_000
    assert set(view) == {
        "target",
        "success",
        "throughput_mbps",
        "peak_mbps",
        "bytes_transferred",
        "duration_s",
        "sample_count",
    }


def test_summarize_flat_json_view_for_failure(monkeypatch):
    def stream(url: str, timeout: float):
        raise httpx.ConnectError("nope")
        yield b""  # pragma: no cover

    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", stream)
    result = bandwidth_probe.measure("https://speed.example/down")

    view = bandwidth_probe.summarize(result)
    assert view == {
        "target": "https://speed.example/down",
        "success": False,
        "error": "nope",
        "error_type": "http_failure",
    }


def test_summarize_carries_baseline_context_only_when_attached(monkeypatch):
    """The four baseline keys appear exactly when the speed use case
    attached them -- a probe-only measurement keeps the v0.3 shape."""
    chunks = [b"q" * 50_000 for _ in range(4)]
    monkeypatch.setattr(bandwidth_probe, "_iter_download_chunks", _fake_stream(chunks))
    plain = bandwidth_probe.measure("https://speed.example/down", duration_s=0.2)
    assert "verdict" not in bandwidth_probe.summarize(plain)

    annotated = bandwidth_probe.measure("https://speed.example/down", duration_s=0.2)
    annotated.extra.update(
        {
            "baseline_mean_mbps": 90.0,
            "baseline_samples": 4,
            "deviation_pct": -18.2,
            "verdict": "slower_than_usual",
        }
    )
    view = bandwidth_probe.summarize(annotated)
    assert view["baseline_mean_mbps"] == 90.0
    assert view["baseline_samples"] == 4
    assert view["deviation_pct"] == pytest.approx(-18.2)
    assert view["verdict"] == "slower_than_usual"
