"""
Characterization tests for netscope.intelligence.baseline.

These document the CURRENT behavior of MetricBaseline/UserBaseline.
The algorithm itself (Welford's online mean/variance) is not changed
by this task -- these tests exist to protect it during future
integration work (see implementation-audit.md, TASK-008/TASK-009/TASK-010).
"""

from __future__ import annotations

import math

import pytest

from netscope.intelligence.baseline import MetricBaseline, UserBaseline


# ---------------------------------------------------------------------------
# MetricBaseline
# ---------------------------------------------------------------------------

def test_metric_baseline_starts_empty():
    b = MetricBaseline()
    assert b.count == 0
    assert b.mean == 0.0
    assert b.stddev == 0.0


def test_metric_baseline_first_observation_sets_mean_to_that_value():
    b = MetricBaseline()
    b.update(20.0)
    assert b.count == 1
    assert b.mean == 20.0
    # stddev is undefined with a single sample -- current implementation
    # returns 0.0 rather than NaN.
    assert b.stddev == 0.0


def test_metric_baseline_mean_over_multiple_observations():
    b = MetricBaseline()
    for v in [10.0, 20.0, 30.0]:
        b.update(v)
    assert b.count == 3
    assert b.mean == pytest.approx(20.0)


def test_metric_baseline_stddev_matches_sample_stddev():
    values = [10.0, 12.0, 23.0, 23.0, 16.0, 23.0, 21.0, 16.0]
    b = MetricBaseline()
    for v in values:
        b.update(v)

    expected_mean = sum(values) / len(values)
    expected_variance = sum((v - expected_mean) ** 2 for v in values) / (len(values) - 1)
    expected_stddev = math.sqrt(expected_variance)

    assert b.mean == pytest.approx(expected_mean)
    assert b.stddev == pytest.approx(expected_stddev)


def test_metric_baseline_deviation_sigma_is_zero_below_five_samples():
    """Current implementation refuses to compute a deviation until at
    least 5 samples have been observed, regardless of how extreme the
    queried value is."""
    b = MetricBaseline()
    for v in [20.0, 20.0, 20.0, 20.0]:  # only 4 samples
        b.update(v)

    assert b.count == 4
    assert b.deviation_sigma(1000.0) == 0.0
    assert b.is_anomalous(1000.0) is False


def test_metric_baseline_deviation_sigma_zero_when_stddev_zero_even_with_enough_samples():
    """If every observed value is identical, stddev is 0 and
    deviation_sigma is defined as 0.0 (current implementation avoids
    division by zero this way, rather than raising)."""
    b = MetricBaseline()
    for _ in range(6):
        b.update(20.0)

    assert b.count == 6
    assert b.stddev == 0.0
    assert b.deviation_sigma(999.0) == 0.0


def test_metric_baseline_normal_value_has_low_deviation_sigma():
    b = MetricBaseline()
    for v in [18.0, 19.0, 20.0, 21.0, 22.0, 20.0]:
        b.update(v)

    # 20ms is within the normal range already observed
    sigma = b.deviation_sigma(20.0)
    assert abs(sigma) < 1.0


def test_metric_baseline_anomalous_value_has_high_deviation_sigma():
    b = MetricBaseline()
    for v in [18.0, 19.0, 20.0, 21.0, 22.0, 20.0]:
        b.update(v)

    sigma = b.deviation_sigma(500.0)
    assert sigma > 2.5
    assert b.is_anomalous(500.0) is True


# ---------------------------------------------------------------------------
# UserBaseline
# ---------------------------------------------------------------------------

def test_user_baseline_tracks_targets_independently():
    ub = UserBaseline()
    ub.observe_latency("1.1.1.1", 20.0)
    ub.observe_latency("8.8.8.8", 200.0)

    assert ub.latency["1.1.1.1"].mean == pytest.approx(20.0)
    assert ub.latency["8.8.8.8"].mean == pytest.approx(200.0)


def test_user_baseline_latency_deviation_sigma_delegates_to_metric_baseline():
    ub = UserBaseline()
    for v in [18.0, 19.0, 20.0, 21.0, 22.0, 20.0]:
        ub.observe_latency("1.1.1.1", v)

    assert ub.latency_deviation_sigma("1.1.1.1", 500.0) > 2.5


def test_user_baseline_loss_deviation_sigma_for_unseen_target_is_zero():
    """Querying a target that has never been observed creates a fresh,
    empty MetricBaseline on the fly and returns a 0.0 deviation --
    current behavior, not a raised KeyError."""
    ub = UserBaseline()
    assert ub.loss_deviation_sigma("never-seen.example", 50.0) == 0.0
    # the lookup itself has side effects: it creates the entry
    assert "never-seen.example" in ub.packet_loss
