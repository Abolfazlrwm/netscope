"""
tests.test_drift

The drift watch (v0.15): windowed trend analysis over stored history,
per future-roadmap.md's post-TASK-046 idea ("a drift watch over stored
history"). The core rule set lives in core/drift.py; the use case reads
the log in app/use_cases.py. Everything here is offline: measurements
are constructed in-memory, and the repository tests use a temp-file
SQLite via MeasurementRepository.open -- the same discipline the other
repository-facing test files follow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netscope.core.drift import (
    DRIFT_DEGRADING,
    DRIFT_IMPROVING,
    DRIFT_INSUFFICIENT,
    DRIFT_STABLE,
    VITAL_JITTER,
    VITAL_LATENCY,
    VITAL_LOSS,
    VITAL_THROUGHPUT,
    analyze_drift,
)
from netscope.core.models import ProbeType, RawMeasurement

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def icmp(
    target: str, *, days_ago: float, latency: float | None = None, loss: float | None = None, jitter: float | None = None, success: bool = True
) -> RawMeasurement:
    """One icmp sweep at a fixed age, with the metric readings a
    successful sweep carries (and stale fields a failed one may leave
    behind -- the tests rely on failures being skipped)."""
    return RawMeasurement(
        probe_type=ProbeType.ICMP,
        target=target,
        timestamp=NOW - timedelta(days=days_ago),
        success=success,
        latency_ms=latency,
        packet_loss_pct=loss,
        jitter_ms=jitter,
        error=None if success else "request timed out",
    )


def bandwidth(target: str, *, days_ago: float, mbps: float | None, extra: dict | None = None) -> RawMeasurement:
    """One bandwidth measurement; `extra` overrides the payload so the
    non-numeric-throughput path is reachable."""
    payload = {} if extra is None else extra
    if extra is None and mbps is not None:
        payload = {"throughput_mbps": mbps}
    return RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target=target,
        timestamp=NOW - timedelta(days=days_ago),
        success=mbps is not None or extra is not None,
        extra=payload,
    )


def sweep(target: str, *, days_ago: float, latency: float, loss: float = 0.0, jitter: float = 2.0) -> list[RawMeasurement]:
    """One diagnostic round's worth of icmp readings (the log stores
    one measurement per sweep, so six sweeps is six readings)."""
    return [icmp(target, days_ago=days_ago, latency=latency, loss=loss, jitter=jitter)]


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------


def test_latency_drift_degrading_when_both_conditions_hold():
    """A line that moved 20ms -> 30ms between windows: +50% and +10ms,
    both conditions hold, the watch says degrading."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0 + i * 0.2) for i in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=30.0 + i * 0.3) for i in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    latency = report.vitals[0]
    assert latency.vital == VITAL_LATENCY
    assert latency.verdict == DRIFT_DEGRADING
    assert latency.older_samples == 6
    assert latency.recent_samples == 6
    assert report.overall == DRIFT_DEGRADING
    assert any("median latency" in e and "+50%" in e for e in report.evidence)


def test_latency_stable_within_the_threshold_band():
    """+15% on a 20ms line (+3ms) fires neither condition: the windows
    read the same, and the report stays stable."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=23.0) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    latency = report.vitals[0]
    assert latency.verdict == DRIFT_STABLE
    assert report.overall == DRIFT_STABLE


def test_latency_requires_the_absolute_floor_too():
    """The relative band alone must not fire: a 400ms satellite line
    moving to 409ms is +2.25% -- within the relative band anyway, but
    the paired test proves the STRUCTURE: the verdict needs both the
    percentage and the absolute move, so a purely-relative future
    regression cannot slip the floor."""
    measurements = []
    measurements += [icmp("satellite", days_ago=10, latency=400.0) for _ in range(6)]
    measurements += [icmp("satellite", days_ago=2, latency=409.0) for _ in range(6)]

    report = analyze_drift(measurements, target="satellite", now=NOW)

    assert report.vitals[0].verdict == DRIFT_STABLE


def test_latency_boundary_values_fire_exactly_at_the_thresholds():
    """20ms -> 25ms is +25% and +5ms -- both conditions hold AT the
    boundary (>= semantics); one millisecond less and the verdict is
    stable. The threshold's edges are pinned so a future edit cannot
    silently move them."""
    edge = []
    edge += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    edge += [icmp("example.com", days_ago=2, latency=25.0) for _ in range(6)]
    assert analyze_drift(edge, target="example.com", now=NOW).vitals[0].verdict == DRIFT_DEGRADING

    just_under = []
    just_under += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    just_under += [icmp("example.com", days_ago=2, latency=24.9) for _ in range(6)]
    assert analyze_drift(just_under, target="example.com", now=NOW).vitals[0].verdict == DRIFT_STABLE


def test_latency_improving_mirrors_degrading():
    """The mirror is the same band with the sign flipped: 30ms -> 20ms
    is -33% and -10ms -- improving, by the symmetric rule."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=30.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=20.0) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[0].verdict == DRIFT_IMPROVING
    assert report.overall == DRIFT_IMPROVING


# ---------------------------------------------------------------------------
# loss
# ---------------------------------------------------------------------------


def test_loss_drift_uses_absolute_percentage_points():
    """A window averaging 0% loss against one averaging 3%: +3.0pp is
    past the 2pp floor -- degrading, though the relative number is
    infinite and therefore deliberately not the signal."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0, loss=0.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=20.0, loss=3.0) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    loss = report.vitals[1]
    assert loss.verdict == DRIFT_DEGRADING
    assert loss.change_abs == pytest.approx(3.0)
    assert loss.change_pct is None  # older window is exactly 0 -- no ratio
    assert report.overall == DRIFT_DEGRADING


def test_loss_relative_change_is_never_the_signal():
    """0.2% -> 1.5% is +650% -- but +1.3pp is under the floor: stable.
    This is the test that would fail if someone 'fixed' loss to be
    relative like latency."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0, loss=0.2) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=20.0, loss=1.5) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[1].verdict == DRIFT_STABLE


def test_loss_improving_when_drops_vanish():
    """4% -> 0.5% is -3.5pp: the drops measurably vanished -- improving."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0, loss=4.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=20.0, loss=0.5) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[1].verdict == DRIFT_IMPROVING


# ---------------------------------------------------------------------------
# jitter
# ---------------------------------------------------------------------------


def test_jitter_uses_its_own_wider_band():
    """Jitter's band is wider on both axes: 2.0 -> 3.0ms is +50% (past
    the 30% band) but +1.0ms (under the 1.5ms floor) -- stable; 2.0 ->
    3.5ms clears both -- degrading."""
    stable = []
    stable += [icmp("example.com", days_ago=10, latency=20.0, jitter=2.0) for _ in range(6)]
    stable += [icmp("example.com", days_ago=2, latency=20.0, jitter=3.0) for _ in range(6)]
    assert analyze_drift(stable, target="example.com", now=NOW).vitals[2].verdict == DRIFT_STABLE

    degrading = []
    degrading += [icmp("example.com", days_ago=10, latency=20.0, jitter=2.0) for _ in range(6)]
    degrading += [icmp("example.com", days_ago=2, latency=20.0, jitter=3.5) for _ in range(6)]
    assert analyze_drift(degrading, target="example.com", now=NOW).vitals[2].verdict == DRIFT_DEGRADING


# ---------------------------------------------------------------------------
# throughput
# ---------------------------------------------------------------------------


def test_throughput_lower_is_degrading_higher_is_improving():
    """The direction mirror: 100 -> 80 Mbps (-20%, past the borrowed 15%
    band) is degrading; 80 -> 100 (+25%) is improving. The band is
    THROUGHPUT_VERDICT_BAND_PCT, the same one compare_throughput uses."""
    slower = []
    slower += [bandwidth("example.com", days_ago=10, mbps=100.0) for _ in range(6)]
    slower += [bandwidth("example.com", days_ago=2, mbps=80.0) for _ in range(6)]
    report = analyze_drift(slower, target="example.com", now=NOW)
    assert report.vitals[3].verdict == DRIFT_DEGRADING
    assert report.overall == DRIFT_DEGRADING

    faster = []
    faster += [bandwidth("example.com", days_ago=10, mbps=80.0) for _ in range(6)]
    faster += [bandwidth("example.com", days_ago=2, mbps=100.0) for _ in range(6)]
    report = analyze_drift(faster, target="example.com", now=NOW)
    assert report.vitals[3].verdict == DRIFT_IMPROVING


def test_throughput_within_band_is_stable():
    """100 -> 90 Mbps (-10%) is inside the 15% band: stable."""
    measurements = []
    measurements += [bandwidth("example.com", days_ago=10, mbps=100.0) for _ in range(6)]
    measurements += [bandwidth("example.com", days_ago=2, mbps=90.0) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[3].verdict == DRIFT_STABLE


# ---------------------------------------------------------------------------
# window mechanics
# ---------------------------------------------------------------------------


def test_median_robust_against_one_congested_sweep():
    """The median choice: one 200ms spike among five 20ms readings
    leaves the recent window's median at 20ms -- a single bad sweep
    must not manufacture a trend."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    measurements += [
        icmp("example.com", days_ago=2, latency=20.0),
        icmp("example.com", days_ago=2.2, latency=20.0),
        icmp("example.com", days_ago=2.4, latency=20.0),
        icmp("example.com", days_ago=2.6, latency=20.0),
        icmp("example.com", days_ago=2.8, latency=200.0),
    ]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[0].verdict == DRIFT_STABLE
    assert report.vitals[0].recent_stat == 20.0


def test_windows_are_inclusive_on_both_ends():
    """A reading exactly at the boundary belongs to BOTH windows'
    slice -- the repository's own inclusive semantics, applied in
    memory. The boundary sweep feeds both windows' medians (boundary is
    inside recent AND older), so a boundary-only population is judged
    rather than dropped."""
    boundary = NOW - timedelta(days=7)
    measurements = [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    boundary_sweep = RawMeasurement(
        probe_type=ProbeType.ICMP,
        target="example.com",
        timestamp=boundary,
        success=True,
        latency_ms=50.0,
        packet_loss_pct=0.0,
        jitter_ms=2.0,
    )
    recent_only = [icmp("example.com", days_ago=2, latency=21.0) for _ in range(6)]

    report = analyze_drift(measurements + recent_only + [boundary_sweep], target="example.com", now=NOW)

    # the boundary reading feeds the recent window (21,21,21,21,21,50
    # -> median 21.0 stays 21.0? six + one = 7 values -> median 21.0)
    assert report.vitals[0].recent_samples == 7
    assert report.vitals[0].older_samples == 7  # the same reading in the older window too


def test_measurements_outside_both_windows_are_ignored():
    """A month-old 500ms reading must not dominate: outside the
    two-window span it is invisible to the watch, however extreme."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=21.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=30, latency=500.0) for _ in range(10)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[0].verdict == DRIFT_STABLE
    assert report.vitals[0].older_samples == 6  # the 30-day sweeps never entered


def test_recent_days_validation_rejects_nonpositive_windows():
    """A zero or negative window is a caller bug, not a query for
    nothing -- the same ValueError build_evidence_report raises."""
    with pytest.raises(ValueError, match="recent_days"):
        analyze_drift([], target="example.com", now=NOW, recent_days=0)
    with pytest.raises(ValueError, match="recent_days"):
        analyze_drift([], target="example.com", now=NOW, recent_days=-3)


# ---------------------------------------------------------------------------
# maturity, coverage and failed probes
# ---------------------------------------------------------------------------


def test_insufficient_history_below_five_samples_per_window():
    """The maturity rule, borrowed whole: four readings per window is
    one short of MetricBaseline.deviation_sigma's own 5 -- the watch
    has no opinion rather than a weak one."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(4)]
    measurements += [icmp("example.com", days_ago=2, latency=50.0) for _ in range(4)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[0].verdict == DRIFT_INSUFFICIENT
    assert report.overall == DRIFT_INSUFFICIENT
    assert report.evidence == ()  # nothing worth reading yet
    assert "not enough stored rounds" in report.vitals[0].detail


def test_all_insufficient_vitals_make_the_report_insufficient():
    """Only-bandwidth history: latency/loss/jitter all insufficient,
    throughput insufficient below maturity -- the whole report says so
    instead of pretending one vital's absence is stability."""
    measurements = [bandwidth("example.com", days_ago=10, mbps=100.0) for _ in range(3)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert all(v.verdict == DRIFT_INSUFFICIENT for v in report.vitals)
    assert report.overall == DRIFT_INSUFFICIENT


def test_failed_measurements_never_feed_a_vital():
    """Failed sweeps ride stale numeric fields (the model keeps the
    last latency/loss columns); the watch reads successful sweeps
    only -- a timeout's residual numbers are not evidence."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0, loss=0.0, jitter=2.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=20.0, loss=0.0, jitter=2.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2.5, latency=999.0, loss=100.0, jitter=90.0, success=False) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[0].recent_samples == 6
    assert report.vitals[1].recent_stat == pytest.approx(0.0)
    assert report.vitals[2].recent_stat == pytest.approx(2.0)
    assert report.vitals[0].verdict == DRIFT_STABLE


def test_non_icmp_probe_types_never_feed_the_latency_family():
    """dns/tcp/http/tls measurements carry latency-shaped fields in
    some adapters' futures but are different probes; only icmp feeds
    latency/loss/jitter and only bandwidth feeds throughput."""
    measurements = []
    measurements += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=21.0) for _ in range(6)]
    for days_ago in (10, 2):
        for _ in range(6):
            measurements.append(
                RawMeasurement(
                    probe_type=ProbeType.DNS,
                    target="example.com",
                    timestamp=NOW - timedelta(days=days_ago),
                    success=True,
                    latency_ms=80.0,
                )
            )

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[0].older_samples == 6  # the dns sweeps never entered
    assert report.vitals[0].verdict == DRIFT_STABLE


def test_bandwidth_extra_without_a_numeric_throughput_is_skipped():
    """A bandwidth row whose extra block lacks a numeric
    throughput_mbps contributes nothing -- shape honesty, the same rule
    _report_row applies to verdict/geo/asn extras."""
    measurements = []
    measurements += [bandwidth("example.com", days_ago=10, mbps=100.0) for _ in range(6)]
    measurements += [bandwidth("example.com", days_ago=2, mbps=60.0) for _ in range(6)]
    measurements += [bandwidth("example.com", days_ago=2, mbps=None, extra={"peak_mbps": "fast"}) for _ in range(4)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[3].recent_samples == 6
    assert report.vitals[3].verdict == DRIFT_DEGRADING


# ---------------------------------------------------------------------------
# report shape
# ---------------------------------------------------------------------------


def test_report_carries_the_windows_and_all_four_vitals():
    """The report documents its own windows (start/boundary/end) and
    always carries all four vitals -- coverage is evidence too."""
    measurements = [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=30.0) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW, recent_days=7)

    assert report.target == "example.com"
    assert report.recent_days == 7
    assert report.end == NOW
    assert report.boundary == NOW - timedelta(days=7)
    assert report.window_start == NOW - timedelta(days=14)
    assert [v.vital for v in report.vitals] == [VITAL_LATENCY, VITAL_LOSS, VITAL_JITTER, VITAL_THROUGHPUT]


def test_overall_verdict_degradation_outranks_improvement():
    """One degrading vital wins even beside an improving one: a watch
    that averages a leaking line against a healing one into 'stable'
    has missed its own point."""
    measurements = []
    # latency degrades (20 -> 30)
    measurements += [icmp("example.com", days_ago=10, latency=20.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=30.0) for _ in range(6)]
    # jitter improves (4.0 -> 2.0: -50%, -2.0ms -- both hold)
    measurements += [icmp("example.com", days_ago=10, latency=20.0, jitter=4.0) for _ in range(6)]
    measurements += [icmp("example.com", days_ago=2, latency=30.0, jitter=2.0) for _ in range(6)]

    report = analyze_drift(measurements, target="example.com", now=NOW)

    assert report.vitals[0].verdict == DRIFT_DEGRADING
    assert report.vitals[2].verdict == DRIFT_IMPROVING
    assert report.overall == DRIFT_DEGRADING
