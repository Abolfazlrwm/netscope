"""
Characterization tests for netscope.intelligence.experience_score.

IMPORTANT: this module currently scores against fixed static thresholds
(LATENCY_GOOD_MS/LATENCY_BAD_MS/LOSS_BAD_PCT), not the personal baseline
in intelligence/baseline.py. The implementation-audit.md flagged this
scoring model as REWRITE-pending (it does not consult the baseline at
all). These tests document the CURRENT behavior only -- they are not an
endorsement of this being the final NetScope scoring model.
"""

from __future__ import annotations

import pytest

from netscope.core.models import ExperienceLevel, ProbeType, RawMeasurement
from netscope.intelligence.experience_score import score_measurements


def _measurement(latency_ms=None, packet_loss_pct=None, success=True):
    return RawMeasurement(
        probe_type=ProbeType.ICMP,
        target="1.1.1.1",
        success=success,
        latency_ms=latency_ms,
        packet_loss_pct=packet_loss_pct,
    )


def test_experience_score_is_high_for_healthy_measurements():
    m = _measurement(latency_ms=15.0, packet_loss_pct=0.0)
    event = score_measurements([m])
    assert event.score == pytest.approx(100.0)
    assert event.level == ExperienceLevel.EXCELLENT


def test_experience_score_decreases_with_high_latency():
    good = score_measurements([_measurement(latency_ms=15.0, packet_loss_pct=0.0)])
    bad = score_measurements([_measurement(latency_ms=200.0, packet_loss_pct=0.0)])
    assert bad.score < good.score


def test_experience_score_is_zero_at_or_above_the_bad_latency_threshold():
    """Current implementation treats latency >= LATENCY_BAD_MS (250) as
    a full-zero latency sub-score."""
    event = score_measurements([_measurement(latency_ms=250.0, packet_loss_pct=0.0)])
    # loss sub-score is 100, latency sub-score is 0 -> average 50
    assert event.score == pytest.approx(50.0)


def test_experience_score_decreases_with_packet_loss():
    good = score_measurements([_measurement(latency_ms=15.0, packet_loss_pct=0.0)])
    lossy = score_measurements([_measurement(latency_ms=15.0, packet_loss_pct=3.0)])
    assert lossy.score < good.score


def test_experience_score_is_zero_at_or_above_the_bad_loss_threshold():
    """Current implementation treats loss >= LOSS_BAD_PCT (5.0) as a
    full-zero loss sub-score."""
    event = score_measurements([_measurement(latency_ms=15.0, packet_loss_pct=5.0)])
    # latency sub-score is 100, loss sub-score is 0 -> average 50
    assert event.score == pytest.approx(50.0)


def test_experience_score_poor_for_measurements_near_both_bad_thresholds():
    event = score_measurements([_measurement(latency_ms=249.0, packet_loss_pct=4.9)])
    assert event.level in (ExperienceLevel.POOR, ExperienceLevel.DEGRADED)
    assert event.score < 40  # current POOR/DEGRADED boundary is 40


def test_experience_score_is_zero_for_a_failed_measurement_regardless_of_numbers():
    """A failed probe (success=False) currently contributes a hard 0.0
    sub-score, even if stale latency/loss numbers happen to be present."""
    m = _measurement(latency_ms=1.0, packet_loss_pct=0.0, success=False)
    event = score_measurements([m])
    assert event.score == 0.0
    assert event.level == ExperienceLevel.DOWN


def test_experience_score_is_down_for_empty_measurement_list():
    """Current behavior for 'no measurements at all': score 0.0, level
    DOWN. This is the current handling of the missing-data case."""
    event = score_measurements([])
    assert event.score == 0.0
    assert event.level == ExperienceLevel.DOWN


def test_experience_score_treats_missing_latency_as_worst_case_latency():
    """A successful measurement with latency_ms=None currently scores
    the latency component as 0 (not ignored, not treated as 'good')."""
    m = _measurement(latency_ms=None, packet_loss_pct=0.0, success=True)
    event = score_measurements([m])
    # latency sub-score 0, loss sub-score 100 -> average 50
    assert event.score == pytest.approx(50.0)


def test_experience_score_treats_missing_packet_loss_as_zero_loss():
    """A successful measurement with packet_loss_pct=None currently
    scores the loss component as a perfect 100 (i.e. 'no loss data'
    is NOT treated the same as 'bad loss')."""
    m = _measurement(latency_ms=15.0, packet_loss_pct=None, success=True)
    event = score_measurements([m])
    assert event.score == pytest.approx(100.0)


def test_experience_score_averages_across_multiple_measurements():
    excellent = _measurement(latency_ms=10.0, packet_loss_pct=0.0)
    down = _measurement(latency_ms=1.0, packet_loss_pct=0.0, success=False)
    event = score_measurements([excellent, down])
    assert event.score == pytest.approx(50.0)
