"""
Tests for netscope.core.routing.

Fully pure and offline: every RouteSnapshot/RouteHop here is hand-
constructed, no traceroute probe or network access is involved,
consistent with the rest of core/'s test suite (test_baseline.py,
test_experience_score.py, test_diagnosis.py, test_ports.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netscope.core.models import RouteHop, RouteSnapshot
from netscope.core.routing import RouteChange, RouteChurnResult, analyze_route_churn


def _hop(address: str, **kwargs) -> RouteHop:
    defaults = dict(ttl=1, hostname=None, avg_rtt_ms=10.0, packet_loss_pct=0.0)
    defaults.update(kwargs)
    return RouteHop(address=address, **defaults)


def _snapshot(target: str, addresses: list[str], timestamp: datetime, **hop_kwargs) -> RouteSnapshot:
    hops = [_hop(addr, ttl=i + 1, **hop_kwargs) for i, addr in enumerate(addresses)]
    return RouteSnapshot(target=target, timestamp=timestamp, hops=hops)


_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Stability / no-change cases
# ---------------------------------------------------------------------------


def test_single_snapshot_is_reported_as_stable_with_no_changes():
    snap = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(0))

    result = analyze_route_churn([snap])

    assert result.target == "example.com"
    assert result.snapshot_count == 1
    assert result.unique_signature_count == 1
    assert result.change_count == 0
    assert result.is_stable is True
    assert result.changes == []


def test_two_identical_route_snapshots_are_stable():
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    assert result.is_stable is True
    assert result.change_count == 0
    assert result.unique_signature_count == 1


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def test_two_different_route_snapshots_produce_one_change():
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.9"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    assert result.is_stable is False
    assert result.change_count == 1
    assert result.unique_signature_count == 2


def test_route_change_captures_before_and_after_signatures_and_timestamps():
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.9"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    assert len(result.changes) == 1
    change = result.changes[0]
    assert isinstance(change, RouteChange)
    assert change.previous_signature == snap_a.signature()
    assert change.new_signature == snap_b.signature()
    assert change.previous_timestamp == _at(0)
    assert change.new_timestamp == _at(5)


def test_multiple_changes_are_all_detected_in_order():
    snap_a = _snapshot("example.com", ["10.0.0.1"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.2"], _at(5))
    snap_c = _snapshot("example.com", ["10.0.0.3"], _at(10))

    result = analyze_route_churn([snap_a, snap_b, snap_c])

    assert result.change_count == 2
    assert result.changes[0].previous_signature == snap_a.signature()
    assert result.changes[0].new_signature == snap_b.signature()
    assert result.changes[1].previous_signature == snap_b.signature()
    assert result.changes[1].new_signature == snap_c.signature()


def test_back_and_forth_route_counts_two_changes_but_only_two_unique_signatures():
    """A -> B -> A: two transitions occurred (2 changes), but only 2
    distinct routes were ever observed (unique_signature_count), not 3
    -- these are deliberately different numbers, both meaningful."""
    snap_a1 = _snapshot("example.com", ["10.0.0.1"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.2"], _at(5))
    snap_a2 = _snapshot("example.com", ["10.0.0.1"], _at(10))

    result = analyze_route_churn([snap_a1, snap_b, snap_a2])

    assert result.change_count == 2
    assert result.unique_signature_count == 2
    assert result.snapshot_count == 3


# ---------------------------------------------------------------------------
# Immunity to lookup-metadata-only changes (builds on TASK-020's guarantee)
# ---------------------------------------------------------------------------


def test_churn_detection_is_unaffected_by_asn_organization_country_changes():
    """A hop's asn/organization/country (TASK-020 fields) being filled
    in or changed by a future lookup, with the address staying
    identical, must NOT register as a route change -- extends
    test_models.py's signature-level guarantee up to the churn-analysis
    level, which is where it actually matters operationally."""
    snap_without_lookup = RouteSnapshot(
        target="example.com",
        timestamp=_at(0),
        hops=[RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=10.0, packet_loss_pct=0.0)],
    )
    snap_with_lookup = RouteSnapshot(
        target="example.com",
        timestamp=_at(5),
        hops=[
            RouteHop(
                ttl=1,
                address="10.0.0.1",
                hostname="router.example.com",
                avg_rtt_ms=11.0,
                packet_loss_pct=0.0,
                asn="AS64500",
                organization="Example ISP",
                country="US",
            )
        ],
    )

    result = analyze_route_churn([snap_without_lookup, snap_with_lookup])

    assert result.is_stable is True
    assert result.change_count == 0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_snapshot_list_raises_value_error():
    with pytest.raises(ValueError):
        analyze_route_churn([])


def test_mismatched_targets_raises_value_error():
    snap_a = _snapshot("example.com", ["10.0.0.1"], _at(0))
    snap_b = _snapshot("other.example.com", ["10.0.0.2"], _at(5))

    with pytest.raises(ValueError):
        analyze_route_churn([snap_a, snap_b])


# ---------------------------------------------------------------------------
# RouteChurnResult itself
# ---------------------------------------------------------------------------


def test_route_churn_result_snapshot_count_matches_input_length():
    snapshots = [_snapshot("example.com", ["10.0.0.1"], _at(i)) for i in range(5)]

    result = analyze_route_churn(snapshots)

    assert result.snapshot_count == 5


def test_route_churn_result_does_not_decide_diagnosis():
    """Confirms RouteChurnResult carries only observational fields
    (target/counts/changes/window bounds) -- no severity, no verdict, no
    diagnosis-shaped field -- per Routing's 'must not decide the final
    diagnosis classification' responsibility. This is checked
    structurally by listing the dataclass's own fields rather than just
    trusting the docstring."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(RouteChurnResult)}
    assert field_names == {
        "target",
        "snapshot_count",
        "unique_signature_count",
        "changes",
        "window_start",
        "window_end",
    }


# ---------------------------------------------------------------------------
# Hop-level diffs (v0.8) -- WHAT changed, not just THAT something did
# ---------------------------------------------------------------------------


def test_single_address_change_names_the_moved_hop():
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2", "10.0.0.3"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.2", "10.0.0.9"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    change = result.last_change
    assert change is not None
    assert [(h.ttl, h.previous_address, h.new_address) for h in change.changed_hops] == [(3, "10.0.0.3", "10.0.0.9")]


def test_unrelated_hops_do_not_appear_in_the_diff():
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2", "198.51.100.7"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.2", "198.51.100.9"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    change = result.last_change
    assert change is not None
    assert len(change.changed_hops) == 1  # only hop 3 moved, hops 1-2 stay out of the diff


def test_star_hops_count_as_changes_when_a_hop_stops_answering():
    """A hop that answered last run and timed out this run changes the
    signature (None addresses join it as '*') -- the diff must show the
    address dropped to None, not hide behind a generic 'changed'."""
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", None], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    assert result.is_stable is False
    change = result.last_change
    assert change is not None
    assert [(h.ttl, h.previous_address, h.new_address) for h in change.changed_hops] == [(2, "10.0.0.2", None)]


def test_growing_path_reports_added_hops():
    snap_a = _snapshot("example.com", ["10.0.0.1"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.2", "10.0.0.3"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    change = result.last_change
    assert change is not None
    assert [(h.ttl, h.previous_address, h.new_address) for h in change.changed_hops] == [
        (2, None, "10.0.0.2"),
        (3, None, "10.0.0.3"),
    ]


def test_shrinking_path_reports_removed_hops():
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2", "10.0.0.3"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    change = result.last_change
    assert change is not None
    assert [(h.ttl, h.previous_address, h.new_address) for h in change.changed_hops] == [
        (2, "10.0.0.2", None),
        (3, "10.0.0.3", None),
    ]


def test_stable_snapshots_carry_no_hop_diffs():
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(5))

    result = analyze_route_churn([snap_a, snap_b])

    assert result.last_change is None
    for change in result.changes:
        assert change.changed_hops == []


def test_each_change_diffs_only_against_its_immediate_predecessor():
    """Three snapshots A -> B -> C: the first change diffs A against B,
    the second B against C -- a hop restored between B and C must not
    leak into the first change's diff."""
    snap_a = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1", "10.0.0.9"], _at(5))
    snap_c = _snapshot("example.com", ["10.0.0.1", "10.0.0.2"], _at(10))

    result = analyze_route_churn([snap_a, snap_b, snap_c])

    assert result.change_count == 2
    assert [(h.ttl, h.previous_address, h.new_address) for h in result.changes[0].changed_hops] == [(2, "10.0.0.2", "10.0.0.9")]
    assert [(h.ttl, h.previous_address, h.new_address) for h in result.changes[1].changed_hops] == [(2, "10.0.0.9", "10.0.0.2")]


def test_window_bounds_span_the_observed_snapshots():
    snap_a = _snapshot("example.com", ["10.0.0.1"], _at(0))
    snap_b = _snapshot("example.com", ["10.0.0.1"], _at(5))
    snap_c = _snapshot("example.com", ["10.0.0.2"], _at(11))

    result = analyze_route_churn([snap_a, snap_b, snap_c])

    assert result.window_start == _at(0)
    assert result.window_end == _at(11)
    assert result.last_change is not None
    assert result.last_change.previous_timestamp == _at(5)


def test_last_change_returns_none_when_stable():
    snap = _snapshot("example.com", ["10.0.0.1"], _at(0))
    result = analyze_route_churn([snap])
    assert result.last_change is None
