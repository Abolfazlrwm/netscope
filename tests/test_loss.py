"""
Tests for netscope.core.loss.

Fully pure and offline: every RawMeasurement here is hand-constructed,
no probe adapter or network access is involved -- consistent with the
rest of core/'s test suite (test_routing.py, test_baseline.py,
test_diagnosis.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netscope.core.loss import LOSS_CONFIRMATION_ROUNDS, merge_loss_rounds, needs_loss_confirmation
from netscope.core.models import ProbeType, RawMeasurement

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


def _icmp(
    target: str = "1.1.1.1",
    *,
    success: bool = True,
    loss: float | None = None,
    latency: float | None = 10.0,
    jitter: float | None = 1.0,
    extra: dict | None = None,
    timestamp: datetime | None = None,
) -> RawMeasurement:
    return RawMeasurement(
        probe_type=ProbeType.ICMP,
        target=target,
        timestamp=timestamp if timestamp is not None else _T0,
        success=success,
        latency_ms=latency,
        packet_loss_pct=loss,
        jitter_ms=jitter,
        extra=extra if extra is not None else {},
    )


# ---------------------------------------------------------------------------
# needs_loss_confirmation
# ---------------------------------------------------------------------------


def test_partial_loss_on_a_successful_sweep_needs_confirmation():
    assert needs_loss_confirmation(_icmp(loss=25.0))


def test_zero_loss_needs_no_confirmation():
    assert not needs_loss_confirmation(_icmp(loss=0.0))


def test_total_loss_is_a_finding_not_a_suspicion():
    # success=False means nothing answered -- the dead-target case the
    # adapters already report with structured errors, never re-run here.
    assert not needs_loss_confirmation(_icmp(success=False, loss=100.0, latency=None))


def test_clean_success_without_a_loss_figure_needs_no_confirmation():
    # Probes that don't report loss at all (HTTP, DNS) never trigger.
    assert not needs_loss_confirmation(_icmp(loss=None))


def test_a_merged_window_is_never_reconfirmed():
    merged = merge_loss_rounds([_icmp(loss=25.0), _icmp(loss=0.0), _icmp(loss=0.0)])
    assert not needs_loss_confirmation(merged)


def test_confirmation_rounds_constant_is_two():
    # Two extra sweeps: the minimum that separates a stray drop from a
    # live problem, without tripling every lossy check's wall-clock.
    assert LOSS_CONFIRMATION_ROUNDS == 2


# ---------------------------------------------------------------------------
# merge_loss_rounds -- window aggregates
# ---------------------------------------------------------------------------


def test_merge_reports_the_mean_loss_across_the_window():
    merged = merge_loss_rounds([_icmp(loss=50.0), _icmp(loss=0.0), _icmp(loss=25.0)])
    assert merged.packet_loss_pct == pytest.approx(25.0)


def test_merge_averages_latency_and_jitter_over_answering_rounds():
    merged = merge_loss_rounds([_icmp(loss=50.0, latency=10.0, jitter=1.0), _icmp(loss=0.0, latency=20.0, jitter=3.0)])
    assert merged.latency_ms == pytest.approx(15.0)
    assert merged.jitter_ms == pytest.approx(2.0)


def test_merge_is_successful_if_any_round_got_an_answer():
    merged = merge_loss_rounds([_icmp(loss=75.0), _icmp(success=False, loss=None, latency=None)])
    assert merged.success


def test_a_fully_failed_window_keeps_the_first_rounds_error():
    failed = _icmp(success=False, loss=None, latency=None)
    failed.error = "destination unreachable"
    merged = merge_loss_rounds([failed, failed])
    assert not merged.success
    assert merged.error == "destination unreachable"


def test_a_window_with_one_answering_round_has_no_error():
    ok = _icmp(loss=50.0)
    dead = _icmp(success=False, loss=None, latency=None)
    dead.error = "timeout"
    merged = merge_loss_rounds([dead, ok])
    assert merged.success
    assert merged.error is None


def test_merge_stamps_the_last_rounds_timestamp():
    merged = merge_loss_rounds([_icmp(loss=25.0, timestamp=_at(0)), _icmp(loss=0.0, timestamp=_at(30))])
    assert merged.timestamp == _at(30)


def test_merge_carries_the_probe_type_and_target_from_the_first_round():
    merged = merge_loss_rounds([_icmp(target="gateway"), _icmp(target="gateway")])
    assert merged.probe_type is ProbeType.ICMP
    assert merged.target == "gateway"


def test_merge_on_empty_rounds_is_rejected():
    with pytest.raises(ValueError):
        merge_loss_rounds([])


def test_merge_never_mutates_the_rounds_it_is_given():
    original = _icmp(loss=25.0, extra={"min_rtt_ms": 9.0, "max_rtt_ms": 11.0, "packets_sent": 4, "packets_received": 3})
    original.extra["adapter_specific"] = {"keep": True}
    before = dict(original.extra)
    merge_loss_rounds([original, _icmp(loss=0.0, extra=dict(original.extra))])
    assert original.extra == before


# ---------------------------------------------------------------------------
# merge_loss_rounds -- the extra story
# ---------------------------------------------------------------------------


def test_confirmed_loss_when_a_confirmation_sweep_also_loses_packets():
    merged = merge_loss_rounds([_icmp(loss=25.0), _icmp(loss=0.0), _icmp(loss=50.0)])
    assert merged.extra["loss_confirmed"] is True


def test_transient_loss_when_every_confirmation_sweep_is_clean():
    merged = merge_loss_rounds([_icmp(loss=25.0), _icmp(loss=0.0), _icmp(loss=0.0)])
    assert merged.extra["loss_confirmed"] is False


def test_extra_records_the_per_round_losses_in_run_order():
    merged = merge_loss_rounds([_icmp(loss=25.0), _icmp(loss=0.0), _icmp(loss=12.5)])
    assert merged.extra["loss_rounds"] == [25.0, 0.0, 12.5]
    assert merged.extra["loss_window_rounds"] == 3


def test_single_round_window_confirms_nothing():
    # One round, no confirmations: nothing reproduced, so the honest
    # verdict is "not confirmed" -- and needs_loss_confirmation would
    # never have routed a caller here anyway.
    merged = merge_loss_rounds([_icmp(loss=25.0)])
    assert merged.extra["loss_confirmed"] is False
    assert merged.extra["loss_rounds"] == [25.0]


def test_window_level_rtt_and_packet_counts_reaggregate():
    first = _icmp(loss=25.0, extra={"min_rtt_ms": 9.0, "max_rtt_ms": 11.0, "packets_sent": 4, "packets_received": 3})
    second = _icmp(loss=0.0, extra={"min_rtt_ms": 10.0, "max_rtt_ms": 12.0, "packets_sent": 4, "packets_received": 4})
    merged = merge_loss_rounds([first, second])
    assert merged.extra["min_rtt_ms"] == 9.0
    assert merged.extra["max_rtt_ms"] == 12.0
    assert merged.extra["packets_sent"] == 8
    assert merged.extra["packets_received"] == 7


def test_adapter_specific_extras_survive_the_merge():
    merged = merge_loss_rounds([_icmp(loss=25.0, extra={"resolved_ip": "1.1.1.1"}), _icmp(loss=0.0)])
    assert merged.extra["resolved_ip"] == "1.1.1.1"
