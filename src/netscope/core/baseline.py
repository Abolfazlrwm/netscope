"""
Baseline learning.

This is NetScope's own logic (Differentiator #2 from the research doc):
instead of a fixed global threshold (e.g. "alert if latency > 100ms", as
classic NMS tools like Zabbix/Icinga do), we learn what "normal" looks
like for THIS user's network, and flag statistically significant
deviations from it.

Deliberately simple for the MVP: running mean + standard deviation per
metric. This can later be bucketed by hour-of-day/day-of-week without
changing the public interface.

v0.4 adds throughput to the same treatment, with one structural
difference: a download measurement is deliberately segregated from the
latency/loss metrics (bandwidth_probe.py's own docstring explains why a
bandwidth result can never pose as a latency reading), so it gets its
own store on UserBaseline and its own table in persistence -- and its
comparison is percentage-based rather than sigma-based, because "your
download is 30% below usual" is the sentence a human actually acts on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class MetricBaseline:
    """Online (streaming) mean/variance tracker -- Welford's algorithm."""

    count: int = 0
    mean: float = 0.0
    _m2: float = 0.0

    def update(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self._m2 += delta * delta2

    @property
    def stddev(self) -> float:
        if self.count < 2:
            return 0.0
        return math.sqrt(self._m2 / (self.count - 1))

    def deviation_sigma(self, value: float) -> float:
        """How many standard deviations `value` is above the mean.

        Returns 0 if we don't have enough history yet or stddev is 0
        (can't meaningfully say something is "abnormal" from 1-2 samples).
        """
        if self.count < 5 or self.stddev == 0:
            return 0.0
        return (value - self.mean) / self.stddev

    def is_anomalous(self, value: float, sigma_threshold: float = 2.5) -> bool:
        return self.deviation_sigma(value) >= sigma_threshold


# A speed test whose median lands within this percentage of the learned
# mean counts as "as usual"; beyond it, "slower/faster than usual". Wide
# enough that ordinary ISP jitter does not cry wolf, tight enough to be
# worth a sentence in the panel.
THROUGHPUT_VERDICT_BAND_PCT = 15.0

# Throughput verdicts -- the machine-readable values the JSON output
# carries and speed_panel translates into human words.
VERDICT_AS_USUAL = "as_usual"
VERDICT_SLOWER = "slower_than_usual"
VERDICT_FASTER = "faster_than_usual"


@dataclass
class ThroughputComparison:
    """One speed test seen against the learned throughput baseline.

    Produced only once the baseline has `min_samples` observations
    (compare_throughput's default: 3) -- before that, NetScope has no
    opinion worth rendering. `deviation_pct` is signed: negative means
    the new measurement is below the learned mean.
    """

    baseline_mean_mbps: float
    baseline_samples: int
    deviation_pct: float
    verdict: str


@dataclass
class UserBaseline:
    """Per-user, per-target baselines for the metrics we care about."""

    latency: dict[str, MetricBaseline] = field(default_factory=dict)
    packet_loss: dict[str, MetricBaseline] = field(default_factory=dict)
    # v0.4: download throughput, deliberately its own store -- never
    # merged with the latency/loss metrics it must not pose as.
    throughput: dict[str, MetricBaseline] = field(default_factory=dict)

    def _get(self, store: dict[str, MetricBaseline], target: str) -> MetricBaseline:
        if target not in store:
            store[target] = MetricBaseline()
        return store[target]

    def observe_latency(self, target: str, latency_ms: float) -> None:
        self._get(self.latency, target).update(latency_ms)

    def observe_packet_loss(self, target: str, loss_pct: float) -> None:
        self._get(self.packet_loss, target).update(loss_pct)

    def latency_deviation_sigma(self, target: str, latency_ms: float) -> float:
        return self._get(self.latency, target).deviation_sigma(latency_ms)

    def loss_deviation_sigma(self, target: str, loss_pct: float) -> float:
        return self._get(self.packet_loss, target).deviation_sigma(loss_pct)

    def observe_throughput(self, target: str, mbps: float) -> None:
        self._get(self.throughput, target).update(mbps)

    def throughput_baseline(self, target: str) -> MetricBaseline | None:
        """The learned throughput baseline for `target`, or None when
        no speed test for it has ever been observed."""
        return self.throughput.get(target)

    def throughput_deviation_sigma(self, target: str, mbps: float) -> float:
        return self._get(self.throughput, target).deviation_sigma(mbps)


def compare_throughput(
    baseline: UserBaseline,
    target: str,
    mbps: float,
    *,
    min_samples: int = 3,
) -> ThroughputComparison | None:
    """Compare one download result against the learned baseline for
    `target`, or return None when the baseline is too young to have an
    opinion (fewer than `min_samples` observations, or a degenerate
    zero-mean that would make the percentage meaningless).

    The comparison happens BEFORE the new value is observed into the
    baseline -- a measurement is judged against what was known going
    in, then folded in, in that order (the same order
    run_measurement_round uses for latency).
    """
    learned = baseline.throughput_baseline(target)
    if learned is None or learned.count < min_samples or learned.mean <= 0:
        return None
    deviation_pct = (mbps - learned.mean) / learned.mean * 100.0
    if deviation_pct <= -THROUGHPUT_VERDICT_BAND_PCT:
        verdict = VERDICT_SLOWER
    elif deviation_pct >= THROUGHPUT_VERDICT_BAND_PCT:
        verdict = VERDICT_FASTER
    else:
        verdict = VERDICT_AS_USUAL
    return ThroughputComparison(
        baseline_mean_mbps=learned.mean,
        baseline_samples=learned.count,
        deviation_pct=deviation_pct,
        verdict=verdict,
    )
