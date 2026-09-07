"""
netscope.core.scoring

Baseline-driven experience/health scoring (TASK-025).

Turns a set of RawMeasurements into a single 0-100 "how good does the
internet feel right now" ExperienceEvent, per
docs/architecture/architecture-overview.md SS10:

    Raw Metrics -> Baseline -> Metric Deviation -> Metric Scores ->
    Weighted Experience Score

This replaces the previous provisional model
(netscope.intelligence.experience_score), which scored every user
against the same fixed global latency/packet-loss thresholds
(LATENCY_GOOD_MS/LATENCY_BAD_MS/LOSS_BAD_PCT). NetScope's differentiator
is that "normal" is learned per user, per target -- 100ms may be a
significant regression for someone on fibre and unremarkable for
someone on a congested mobile hotspot. This module consults the
caller-supplied netscope.core.baseline.UserBaseline instead of any
universal constant.

WHY THIS LIVES IN core
------------------------
Pure domain computation: no I/O, no adapters, no persistence, no UI.
The only imports are the Python standard library and netscope.core
siblings (models, baseline), per adr-001-architecture-style.md and the
same dependency-direction rule core/ports.py documents for itself.

WHY THIS MODULE DOES NOT LOAD OR SAVE THE BASELINE
-----------------------------------------------------
architecture-overview.md SS10 places baseline load/observe/persist
wiring in a future app/use_cases.py, via the BaselineRepository port
(TASK-024) -- not in scoring. This module receives an already-loaded
UserBaseline as a plain argument and returns a result; it never reads
from or writes to a BaselineRepository, and it never mutates the
UserBaseline it is given (it only reads MetricBaseline.count/mean/
stddev and calls MetricBaseline.deviation_sigma(), never
UserBaseline.observe_*/_get, which are the only baseline methods that
have side effects). Callers remain responsible for observing new
values into the baseline and persisting it -- that stays entirely
outside this module's scope, exactly as instructed by TASK-025.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from netscope.core.baseline import MetricBaseline, UserBaseline
from netscope.core.models import ExperienceEvent, ExperienceLevel, RawMeasurement, utcnow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#
# Neither of the two numbers below is a new arbitrary threshold invented by
# this module. Both are read directly from decisions netscope.core.baseline
# has *already* made and that this task is explicitly not allowed to change:
#
# - MetricBaseline.deviation_sigma() already refuses to produce a real
#   signal (returns a defined 0.0 instead) below 5 samples. This module
#   reuses that same "5" as its own maturity cutoff for consistency --
#   below it, core/baseline.py itself has no opinion yet, so scoring
#   shouldn't pretend to either.
# - MetricBaseline.is_anomalous()'s own default sigma_threshold is 2.5.
#   This module anchors its score-vs-deviation curve so that a score of
#   0 is reached exactly at the point core/baseline.py already calls a
#   value "anomalous", rather than picking a new, unrelated cutoff.
_MATURE_SAMPLE_COUNT = 5
_ANOMALY_SIGMA_THRESHOLD = 2.5

_FAILED_MEASUREMENT_SCORE = 0.0
_NEUTRAL_SCORE = 100.0


@dataclass(frozen=True)
class MetricEvaluation:
    """The baseline-relative evaluation of a single metric (latency or
    packet loss) for a single measurement.

    `confidence` (0.0-1.0) says how much weight this evaluation should
    carry in the aggregate score -- low for a target/metric with little
    or no baseline history, full once the baseline is mature. This is
    what lets "insufficient history" and "genuinely normal" both surface
    a sigma-like reading of 0 without being treated identically: an
    immature reading gets a low-confidence, neutral score instead of
    being asserted as healthy.
    """

    score: float
    confidence: float


def _evaluate_metric(
    store: dict[str, MetricBaseline], target: str, value: Optional[float]
) -> MetricEvaluation:
    """Evaluate one metric value for `target` against its MetricBaseline
    in `store` (either a UserBaseline's `.latency` or `.packet_loss`
    dict). Read-only: never creates or mutates entries in `store`.
    """
    if value is None:
        # The measurement itself didn't produce this metric (e.g. a
        # probe type that doesn't report packet loss). There's nothing
        # to evaluate -- zero confidence, neutral score so it neither
        # drags down nor inflates the aggregate.
        return MetricEvaluation(score=_NEUTRAL_SCORE, confidence=0.0)

    metric = store.get(target)

    if metric is None or metric.count < _MATURE_SAMPLE_COUNT:
        # Insufficient history: distinct from "value matches the mean".
        # We don't yet have enough evidence to call `value` normal or
        # abnormal for this target, so we make no claim (neutral score)
        # and scale confidence by how much history actually exists --
        # 0.0 with none, ramping linearly to full confidence at
        # _MATURE_SAMPLE_COUNT samples (architecture-overview.md SS10's
        # "Confidence" guidance).
        count = metric.count if metric is not None else 0
        confidence = min(count / _MATURE_SAMPLE_COUNT, 1.0)
        return MetricEvaluation(score=_NEUTRAL_SCORE, confidence=confidence)

    if metric.stddev == 0.0:
        # Mature (>= _MATURE_SAMPLE_COUNT samples) but has never varied.
        # MetricBaseline.deviation_sigma() is defined to return 0.0 here
        # too (division-by-zero guard) -- which would be indistinguishable
        # from "value == mean" if we relied on it. So this case is
        # checked directly: a value that finally differs from an
        # otherwise perfectly stable target is real evidence of change,
        # not something to wave through as healthy.
        if value == metric.mean:
            return MetricEvaluation(score=_NEUTRAL_SCORE, confidence=1.0)
        return MetricEvaluation(score=0.0, confidence=1.0)

    sigma = metric.deviation_sigma(value)
    if sigma <= 0:
        # At or below the personal mean. For latency/packet-loss, lower
        # is never worse than usual, so this is unambiguously healthy.
        return MetricEvaluation(score=_NEUTRAL_SCORE, confidence=1.0)

    # Linear falloff anchored at core/baseline.py's own anomaly
    # threshold: score reaches 0 exactly at sigma ==
    # _ANOMALY_SIGMA_THRESHOLD, matching what is_anomalous() would
    # already call abnormal, and 100 at sigma == 0.
    score = max(0.0, _NEUTRAL_SCORE * (1 - sigma / _ANOMALY_SIGMA_THRESHOLD))
    return MetricEvaluation(score=score, confidence=1.0)


def _level_for_score(score: float) -> ExperienceLevel:
    """Maps an already-computed 0-100 score to the existing
    ExperienceLevel buckets. Unchanged from the previous
    experience_score.py -- this is a fixed mapping of a *score* to a
    *level*, not one of the static per-metric thresholds TASK-025 is
    scoped to retire, so there's no architectural reason to change it.
    """
    if score >= 90:
        return ExperienceLevel.EXCELLENT
    if score >= 70:
        return ExperienceLevel.GOOD
    if score >= 40:
        return ExperienceLevel.DEGRADED
    if score > 0:
        return ExperienceLevel.POOR
    return ExperienceLevel.DOWN


def score_measurements(measurements: list[RawMeasurement], baseline: UserBaseline) -> ExperienceEvent:
    """Combine a batch of measurements (e.g. one round of icmp+dns+http
    probes) into a single ExperienceEvent, scored against `baseline`
    instead of any fixed global threshold.

    `baseline` is read-only from this function's point of view: no
    UserBaseline.observe_*/_get call is ever made here, so calling this
    does not record `measurements` into `baseline` as history. Observing
    new values and persisting the baseline are the caller's
    responsibility (architecture-overview.md SS10's `app` use-case
    wiring), not scoring's.

    Note on the required `baseline` parameter: the previous
    `score_measurements(measurements)` signature (no baseline) is
    intentionally not preserved -- a scoring function that cannot
    consult a personal baseline can't implement TASK-025's goal at all,
    so this is the one deliberate, minimal public-API break this task
    makes.
    """
    if not measurements:
        # No measurements at all is not "no opinion" (that's the
        # zero-confidence case below, for measurements that exist but
        # carry no usable baseline evidence) -- it's the worst
        # diagnosable state, same as the previous implementation.
        return ExperienceEvent(timestamp=utcnow(), score=0.0, level=ExperienceLevel.DOWN)

    evaluations: list[MetricEvaluation] = []
    for m in measurements:
        if not m.success:
            # A failed probe is never healthy regardless of any stale
            # latency/loss numbers that might still be present on the
            # object -- full confidence, since a failure is unambiguous
            # evidence, not a metric reading to weigh probabilistically.
            evaluations.append(MetricEvaluation(score=_FAILED_MEASUREMENT_SCORE, confidence=1.0))
            continue
        evaluations.append(_evaluate_metric(baseline.latency, m.target, m.latency_ms))
        evaluations.append(_evaluate_metric(baseline.packet_loss, m.target, m.packet_loss_pct))

    total_confidence = sum(e.confidence for e in evaluations)
    if total_confidence == 0.0:
        # Every metric across every measurement was zero-confidence --
        # e.g. all brand-new targets with no baseline history yet, and
        # no failed probes to anchor a definite signal either way.
        # Neutral score: there is no usable evidence to call this
        # anything but unknown, and unknown should not read as
        # unhealthy.
        overall = _NEUTRAL_SCORE
    else:
        overall = sum(e.score * e.confidence for e in evaluations) / total_confidence

    return ExperienceEvent(
        timestamp=utcnow(),
        score=round(overall, 1),
        level=_level_for_score(overall),
        contributing_measurements=measurements,
    )
