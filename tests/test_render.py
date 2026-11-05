"""Tests for netscope.ui.render's speed panel -- the user-facing side
of the v0.4 baseline-relative verdict: the comparison line appears
exactly when the use case attached one, with a human-readable label."""

from __future__ import annotations

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement
from netscope.ui.render import speed_panel


def _speed_measurement(extra: dict) -> RawMeasurement:
    return RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target="https://speed.example/down",
        success=True,
        extra=extra,
    )


def test_speed_panel_renders_the_verdict_line_when_attached():
    panel = speed_panel(
        _speed_measurement(
            {
                "throughput_mbps": 73.6,
                "peak_mbps": 88.0,
                "baseline_mean_mbps": 90.0,
                "baseline_samples": 4,
                "deviation_pct": -18.2,
                "verdict": "slower_than_usual",
            }
        )
    )
    plain = panel.renderable.plain
    assert "vs your usual 90.0 Mbps" in plain
    assert "-18.2%" in plain
    assert "slower than usual" in plain


def test_speed_panel_stays_silent_while_the_baseline_is_young():
    panel = speed_panel(_speed_measurement({"throughput_mbps": 87.4, "peak_mbps": 94.7}))
    assert "vs your usual" not in panel.renderable.plain


def test_speed_panel_failure_path_is_unchanged():
    panel = speed_panel(
        RawMeasurement(
            probe_type=ProbeType.BANDWIDTH,
            target="https://speed.example/down",
            success=False,
            error="HTTP 404",
            error_type=ProbeErrorType.HTTP_FAILURE,
        )
    )
    assert "speed test failed" in panel.renderable.plain
