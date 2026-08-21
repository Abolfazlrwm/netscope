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


@dataclass
class UserBaseline:
    """Per-user, per-target baselines for the metrics we care about."""

    latency: dict[str, MetricBaseline] = field(default_factory=dict)
    packet_loss: dict[str, MetricBaseline] = field(default_factory=dict)

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
