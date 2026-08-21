"""
Experience scoring.

Turns a set of RawMeasurements (icmp/dns/http) into a single 0-100
"how good does the internet feel right now" score. This is NetScope's
own synthesis -- no existing tool in the research doc does this; MTR/
iperf3/speedtest-cli each give you one raw number, not a combined score.
"""

from __future__ import annotations

from netscope.core.models import ExperienceEvent, ExperienceLevel, RawMeasurement, utcnow

# Simple, tunable scoring weights. All measurements degrade the score;
# a failed probe degrades it the most.
LATENCY_GOOD_MS = 40
LATENCY_BAD_MS = 250
LOSS_BAD_PCT = 5.0


def _score_latency(latency_ms: float | None) -> float:
    if latency_ms is None:
        return 0.0
    if latency_ms <= LATENCY_GOOD_MS:
        return 100.0
    if latency_ms >= LATENCY_BAD_MS:
        return 0.0
    # linear interpolation between good and bad
    span = LATENCY_BAD_MS - LATENCY_GOOD_MS
    return 100.0 * (1 - (latency_ms - LATENCY_GOOD_MS) / span)


def _score_loss(loss_pct: float | None) -> float:
    if loss_pct is None:
        return 100.0
    if loss_pct <= 0:
        return 100.0
    if loss_pct >= LOSS_BAD_PCT:
        return 0.0
    return 100.0 * (1 - loss_pct / LOSS_BAD_PCT)


def _level_for_score(score: float) -> ExperienceLevel:
    if score >= 90:
        return ExperienceLevel.EXCELLENT
    if score >= 70:
        return ExperienceLevel.GOOD
    if score >= 40:
        return ExperienceLevel.DEGRADED
    if score > 0:
        return ExperienceLevel.POOR
    return ExperienceLevel.DOWN


def score_measurements(measurements: list[RawMeasurement]) -> ExperienceEvent:
    """Combine a batch of measurements (e.g. one round of icmp+dns+http
    probes) into a single ExperienceEvent."""

    if not measurements:
        return ExperienceEvent(timestamp=utcnow(), score=0.0, level=ExperienceLevel.DOWN)

    sub_scores: list[float] = []
    for m in measurements:
        if not m.success:
            sub_scores.append(0.0)
            continue
        latency_score = _score_latency(m.latency_ms)
        loss_score = _score_loss(m.packet_loss_pct)
        sub_scores.append((latency_score + loss_score) / 2)

    overall = sum(sub_scores) / len(sub_scores)
    return ExperienceEvent(
        timestamp=utcnow(),
        score=round(overall, 1),
        level=_level_for_score(overall),
        contributing_measurements=measurements,
    )
