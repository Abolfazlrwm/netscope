"""
Tests for netscope.core.routing.

Fully pure and offline: every RouteSnapshot/RouteHop here is hand-
constructed, no traceroute probe or network access is involved,
consistent with the rest of core/'s test suite (test_baseline.py,
test_experience_score.py, test_diagnosis.py, test_ports.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
                ttl=1, address="10.0.0.1", hostname="router.example.com",
                avg_rtt_ms=11.0, packet_loss_pct=0.0,
                asn="AS64500", organization="Example ISP", country="US",
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
    (target/counts/changes) -- no severity, no verdict, no diagnosis-
    shaped field -- per Routing's 'must not decide the final diagnosis
    classification' responsibility. This is checked structurally by
    listing the dataclass's own fields rather than just trusting the
    docstring."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(RouteChurnResult)}
    assert field_names == {"target", "snapshot_count", "unique_signature_count", "changes"}
