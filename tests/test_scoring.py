"""
Characterization tests for netscope.core.scoring (TASK-025).

These replace tests/test_experience_score.py, which characterized the
now-removed intelligence/experience_score.py's fixed-global-threshold
model (LATENCY_GOOD_MS/LATENCY_BAD_MS/LOSS_BAD_PCT). That model is
retired by TASK-025, per docs/architecture/future-roadmap.md's TASK-025
row and architecture-overview.md SS10 -- these tests intentionally do
NOT preserve the old threshold-based assertions (e.g. "250ms always
scores 0"), because under the new architecture the same raw latency can
score very differently depending on the caller's own UserBaseline. What
IS preserved from the old suite, because nothing about TASK-025 changes
it, is: the empty-measurement-list behavior (score 0.0/DOWN) and the
ExperienceLevel score-bucket mapping (90/70/40/0).

Fully offline and deterministic: baselines below are hand-constructed
in memory, never observed from real probes.
"""

from __future__ import annotations

import pytest

from netscope.core.baseline import MetricBaseline, UserBaseline
from netscope.core.models import ExperienceLevel, ProbeType, RawMeasurement
from netscope.core.scoring import score_measurements


def _measurement(target="1.1.1.1", latency_ms=None, packet_loss_pct=None, success=True):
    return RawMeasurement(
        probe_type=ProbeType.ICMP,
        target=target,
        success=success,
        latency_ms=latency_ms,
        packet_loss_pct=packet_loss_pct,
    )


def _mature_latency_baseline(target: str, values: list[float]) -> UserBaseline:
    """A UserBaseline with a mature (>=5 sample) latency history for
    `target`, built from `values` via the real Welford update -- not a
    hand-set mean/stddev, so this exercises the actual baseline
    algorithm as scoring will see it in practice."""
    ub = UserBaseline()
    for v in values:
        ub.observe_latency(target, v)
    return ub


# ---------------------------------------------------------------------------
# 1. Same raw latency scores differently against different personal baselines
# ---------------------------------------------------------------------------


def test_same_latency_is_normal_for_a_high_latency_baseline_but_anomalous_for_a_low_latency_one():
    low_latency_history = [10.0, 11.0, 9.0, 10.0, 10.0, 11.0, 9.0]
    high_latency_history = [180.0, 190.0, 185.0, 195.0, 188.0, 192.0, 187.0]

    baseline_low = _mature_latency_baseline("1.1.1.1", low_latency_history)
    baseline_high = _mature_latency_baseline("1.1.1.1", high_latency_history)

    probe = _measurement(target="1.1.1.1", latency_ms=100.0, packet_loss_pct=0.0)

    event_against_low = score_measurements([probe], baseline_low)
    event_against_high = score_measurements([probe], baseline_high)

    # 100ms is a large positive deviation for the low-latency baseline...
    assert event_against_low.score < 40
    # ...but comfortably at/under the mean for the high-latency baseline.
    assert event_against_high.score > 90
    assert event_against_low.score < event_against_high.score


# ---------------------------------------------------------------------------
# 2. Per-target baseline isolation
# ---------------------------------------------------------------------------


def test_baseline_for_one_target_does_not_influence_scoring_of_another_target():
    ub = UserBaseline()
    for v in [10.0, 10.0, 10.0, 10.0, 10.0]:
        ub.observe_latency("fast-target", v)
    for v in [200.0, 200.0, 200.0, 200.0, 200.0]:
        ub.observe_latency("slow-target", v)

    # 200ms is way off "fast-target"'s baseline...
    event_fast = score_measurements([_measurement(target="fast-target", latency_ms=200.0)], ub)
    # ...but exactly at "slow-target"'s own mean.
    event_slow = score_measurements([_measurement(target="slow-target", latency_ms=200.0)], ub)

    assert event_fast.score < event_slow.score
    assert event_slow.score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 3. Mature, normal measurement
# ---------------------------------------------------------------------------


def test_mature_baseline_normal_value_scores_healthy():
    baseline = _mature_latency_baseline("1.1.1.1", [18.0, 19.0, 20.0, 21.0, 22.0, 20.0])
    event = score_measurements([_measurement(latency_ms=20.0, packet_loss_pct=0.0)], baseline)
    assert event.score > 90
    assert event.level == ExperienceLevel.EXCELLENT


# ---------------------------------------------------------------------------
# 4. Mature, abnormal measurement
# ---------------------------------------------------------------------------


def test_mature_baseline_significant_deviation_reduces_score():
    baseline = _mature_latency_baseline("1.1.1.1", [18.0, 19.0, 20.0, 21.0, 22.0, 20.0])
    healthy = score_measurements([_measurement(latency_ms=20.0, packet_loss_pct=0.0)], baseline)
    anomalous = score_measurements([_measurement(latency_ms=500.0, packet_loss_pct=0.0)], baseline)

    assert anomalous.score < healthy.score
    assert anomalous.level in (ExperienceLevel.POOR, ExperienceLevel.DOWN, ExperienceLevel.DEGRADED)


# ---------------------------------------------------------------------------
# 5. Insufficient baseline history is distinct from "genuinely normal"
# ---------------------------------------------------------------------------


def test_insufficient_history_is_not_treated_as_confidently_normal():
    """A brand-new target (no observations yet) reports deviation_sigma()
    == 0.0 from core/baseline.py, same numeric value a mature, exactly-
    on-the-mean measurement would report. Scoring must not treat these
    as equivalent: the immature case should carry near-zero confidence,
    while the mature case is fully confident."""
    fresh = UserBaseline()  # target never observed at all
    mature = _mature_latency_baseline("1.1.1.1", [20.0, 20.0, 20.0, 20.0, 20.0, 20.0])

    event_fresh = score_measurements([_measurement(latency_ms=20.0, packet_loss_pct=0.0)], fresh)
    event_mature = score_measurements([_measurement(latency_ms=20.0, packet_loss_pct=0.0)], mature)

    # Both currently land on a neutral/healthy-looking score (there's no
    # evidence of a problem either way), but they are not reached via
    # the same confidence -- verified directly against the evaluator.
    from netscope.core.scoring import _evaluate_metric

    fresh_eval = _evaluate_metric(fresh.latency, "1.1.1.1", 20.0)
    mature_eval = _evaluate_metric(mature.latency, "1.1.1.1", 20.0)

    assert fresh_eval.confidence == 0.0
    assert mature_eval.confidence == 1.0
    assert event_fresh.score == pytest.approx(100.0)
    assert event_mature.score == pytest.approx(100.0)


def test_partial_history_confidence_scales_with_sample_count():
    from netscope.core.scoring import _evaluate_metric

    ub = UserBaseline()
    for v in [20.0, 20.0]:  # 2 samples, below the 5-sample maturity cutoff
        ub.observe_latency("1.1.1.1", v)

    evaluation = _evaluate_metric(ub.latency, "1.1.1.1", 20.0)
    assert evaluation.confidence == pytest.approx(2 / 5)


# ---------------------------------------------------------------------------
# 6. Zero standard deviation
# ---------------------------------------------------------------------------


def test_zero_variance_baseline_exact_match_scores_healthy():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0, 20.0, 20.0, 20.0, 20.0, 20.0])
    event = score_measurements([_measurement(latency_ms=20.0, packet_loss_pct=0.0)], baseline)
    assert event.score == pytest.approx(100.0)


def test_zero_variance_baseline_any_deviation_is_flagged_abnormal():
    """core/baseline.py's deviation_sigma() returns 0.0 for a zero-
    stddev baseline regardless of how far `value` is from the mean --
    scoring must not rely on that value here, or a first-ever deviation
    from a perfectly stable target would be silently scored as healthy."""
    baseline = _mature_latency_baseline("1.1.1.1", [20.0, 20.0, 20.0, 20.0, 20.0, 20.0])
    assert baseline.latency_deviation_sigma("1.1.1.1", 999.0) == 0.0  # confirms the raw baseline behavior

    event = score_measurements([_measurement(latency_ms=999.0, packet_loss_pct=0.0)], baseline)
    assert event.score < 100.0


# ---------------------------------------------------------------------------
# 7. Packet-loss baseline behavior
# ---------------------------------------------------------------------------


def test_packet_loss_is_scored_against_its_own_baseline_like_latency():
    ub = UserBaseline()
    for v in [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]:
        ub.observe_packet_loss("1.1.1.1", v)

    healthy = score_measurements([_measurement(latency_ms=None, packet_loss_pct=0.0)], ub)
    lossy = score_measurements([_measurement(latency_ms=None, packet_loss_pct=25.0)], ub)

    assert lossy.score < healthy.score


# ---------------------------------------------------------------------------
# 8. Failed measurement
# ---------------------------------------------------------------------------


def test_failed_measurement_is_never_healthy_regardless_of_stale_numbers():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0, 20.0, 20.0, 20.0, 20.0, 20.0])
    m = _measurement(latency_ms=20.0, packet_loss_pct=0.0, success=False)
    event = score_measurements([m], baseline)
    assert event.score == 0.0
    assert event.level == ExperienceLevel.DOWN


# ---------------------------------------------------------------------------
# 9. Empty measurement list
# ---------------------------------------------------------------------------


def test_empty_measurement_list_is_down():
    """Unchanged from the previous implementation: no measurements at
    all is the worst diagnosable state, not a neutral/unknown one."""
    event = score_measurements([], UserBaseline())
    assert event.score == 0.0
    assert event.level == ExperienceLevel.DOWN


# ---------------------------------------------------------------------------
# 10. Multiple measurements
# ---------------------------------------------------------------------------


def test_multiple_measurements_are_aggregated_by_confidence_weighted_average():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0, 20.0, 20.0, 20.0, 20.0, 20.0])
    healthy = _measurement(target="1.1.1.1", latency_ms=20.0, packet_loss_pct=0.0)
    failed = _measurement(target="1.1.1.1", latency_ms=1.0, packet_loss_pct=0.0, success=False)

    event = score_measurements([healthy, failed], baseline)

    # The failed measurement pulls the score down, but the healthy,
    # fully-confident measurement still counts in the average.
    assert 0.0 < event.score < 100.0


def test_multiple_measurements_across_different_targets_each_use_their_own_baseline():
    ub = UserBaseline()
    for v in [10.0, 10.0, 10.0, 10.0, 10.0]:
        ub.observe_latency("fast-target", v)
    for v in [200.0, 200.0, 200.0, 200.0, 200.0]:
        ub.observe_latency("slow-target", v)

    event = score_measurements(
        [
            _measurement(target="fast-target", latency_ms=10.0, packet_loss_pct=0.0),
            _measurement(target="slow-target", latency_ms=200.0, packet_loss_pct=0.0),
        ],
        ub,
    )
    # Both measurements are exactly on their own target's mean -> healthy overall.
    assert event.score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 11. ExperienceLevel semantics preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected_level",
    [
        (95.0, ExperienceLevel.EXCELLENT),
        (90.0, ExperienceLevel.EXCELLENT),
        (89.9, ExperienceLevel.GOOD),
        (70.0, ExperienceLevel.GOOD),
        (69.9, ExperienceLevel.DEGRADED),
        (40.0, ExperienceLevel.DEGRADED),
        (39.9, ExperienceLevel.POOR),
        (0.1, ExperienceLevel.POOR),
        (0.0, ExperienceLevel.DOWN),
    ],
)
def test_level_for_score_bucket_boundaries_unchanged(score, expected_level):
    """The score->level bucket boundaries (90/70/40/0) are a fixed
    mapping of an already-computed score to a domain level -- not one of
    the static per-metric thresholds TASK-025 retires -- so they are
    intentionally unchanged from the previous implementation."""
    from netscope.core.scoring import _level_for_score

    assert _level_for_score(score) == expected_level


# ---------------------------------------------------------------------------
# Baseline mutation guard
# ---------------------------------------------------------------------------


def test_scoring_never_mutates_the_baseline_it_is_given():
    """Scoring must be a pure read against the baseline: no new dict
    entries, no observation counts changing, no wiring of persistence
    from within core/scoring.py."""
    ub = UserBaseline()
    for v in [20.0, 20.0, 20.0, 20.0, 20.0, 20.0]:
        ub.observe_latency("1.1.1.1", v)

    targets_before = set(ub.latency.keys())
    count_before = ub.latency["1.1.1.1"].count

    score_measurements(
        [
            _measurement(target="1.1.1.1", latency_ms=999.0, packet_loss_pct=0.0),
            _measurement(target="never-seen-before.example", latency_ms=5.0, packet_loss_pct=0.0),
        ],
        ub,
    )

    assert set(ub.latency.keys()) == targets_before  # no new target entries created
    assert ub.latency["1.1.1.1"].count == count_before  # no new observations recorded


# ---------------------------------------------------------------------------
# Missing metric value on an otherwise-successful measurement
# ---------------------------------------------------------------------------


def test_missing_latency_on_successful_measurement_is_zero_confidence_not_a_bad_score():
    """Unlike the old implementation (missing latency == worst-case
    latency), a metric the probe simply didn't report has no baseline
    evidence either way -- it must not silently drag the score down."""
    baseline = _mature_latency_baseline("1.1.1.1", [20.0, 20.0, 20.0, 20.0, 20.0, 20.0])
    ub = baseline
    for v in [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]:
        ub.observe_packet_loss("1.1.1.1", v)

    m = _measurement(target="1.1.1.1", latency_ms=None, packet_loss_pct=0.0)
    event = score_measurements([m], ub)
    assert event.score == pytest.approx(100.0)
