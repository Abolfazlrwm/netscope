"""
Characterization tests for netscope.core.models.

Focus: invariants future refactoring could accidentally break, not
exhaustive dataclass coverage.
"""

from __future__ import annotations

from datetime import datetime, timezone

from netscope.core.models import (
    ExperienceEvent,
    ExperienceLevel,
    Incident,
    ProbeErrorType,
    ProbeType,
    RawMeasurement,
    RouteHop,
    RouteSnapshot,
)


def test_raw_measurement_defaults_are_unsuccessful_and_empty():
    """A RawMeasurement built with only the required fields should default
    to success=False and empty/None everywhere else -- callers rely on
    being able to construct a "failed" measurement without specifying
    every field."""
    m = RawMeasurement(probe_type=ProbeType.ICMP, target="1.1.1.1")

    assert m.success is False
    assert m.latency_ms is None
    assert m.packet_loss_pct is None
    assert m.jitter_ms is None
    assert m.error is None
    assert m.error_type is None
    assert m.extra == {}


def test_raw_measurement_timestamp_defaults_to_utc_now():
    m = RawMeasurement(probe_type=ProbeType.DNS, target="example.com")
    assert m.timestamp.tzinfo is not None
    assert m.timestamp.tzinfo == timezone.utc


def test_route_snapshot_signature_uses_hop_addresses_in_order():
    """signature() is used to detect route changes -- it must be a
    deterministic function of hop order and address."""
    snap = RouteSnapshot(
        target="example.com",
        hops=[
            RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=1.0, packet_loss_pct=0.0),
            RouteHop(ttl=2, address="10.0.0.2", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0),
        ],
    )
    assert snap.signature() == "10.0.0.1|10.0.0.2"


def test_route_snapshot_signature_uses_wildcard_for_missing_address():
    snap = RouteSnapshot(
        target="example.com",
        hops=[
            RouteHop(ttl=1, address=None, hostname=None, avg_rtt_ms=None, packet_loss_pct=100.0),
        ],
    )
    assert snap.signature() == "*"


def test_route_snapshot_signature_changes_when_hops_change():
    hop_a = RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=1.0, packet_loss_pct=0.0)
    hop_b = RouteHop(ttl=1, address="10.0.0.9", hostname=None, avg_rtt_ms=1.0, packet_loss_pct=0.0)

    snap_a = RouteSnapshot(target="example.com", hops=[hop_a])
    snap_b = RouteSnapshot(target="example.com", hops=[hop_b])

    assert snap_a.signature() != snap_b.signature()


def test_incident_is_active_when_ended_at_is_none():
    incident = Incident(started_at=datetime.now(timezone.utc))
    assert incident.is_active is True


def test_incident_is_not_active_once_ended_at_is_set():
    now = datetime.now(timezone.utc)
    incident = Incident(started_at=now, ended_at=now)
    assert incident.is_active is False


def test_experience_event_holds_contributing_measurements():
    m = RawMeasurement(probe_type=ProbeType.HTTP, target="example.com", success=True, latency_ms=10.0)
    event = ExperienceEvent(
        timestamp=datetime.now(timezone.utc),
        score=95.0,
        level=ExperienceLevel.EXCELLENT,
        contributing_measurements=[m],
    )
    assert event.contributing_measurements == [m]


def test_probe_error_type_has_exactly_the_task_014_through_018_scoped_values():
    """TASK-014 through TASK-017 pinned ProbeErrorType to prior
    justified values, with this exact test asserting the set so any
    later expansion would fail loudly rather than silently. TASK-018 is
    another conscious expansion: httpx.RequestError (ConnectError,
    ReadError, WriteError, ProtocolError, etc. -- everything except
    TimeoutException, which maps to the existing TIMEOUT) represents a
    genuine HTTP-transport-layer failure with no existing good fit,
    justifying HTTP_FAILURE per architecture-overview.md SS6's
    already-anticipated taxonomy. This test is deliberately updated
    (not silently left failing) to reflect that conscious decision."""
    assert {member.value for member in ProbeErrorType} == {
        "timeout",
        "permission_denied",
        "probe_unavailable",
        "dns_failure",
        "connection_refused",
        "tls_failure",
        "http_failure",
        "unknown",
    }


def test_raw_measurement_error_type_can_be_set_to_a_probe_error_type():
    m = RawMeasurement(
        probe_type=ProbeType.ICMP,
        target="1.1.1.1",
        success=False,
        error="permission error",
        error_type=ProbeErrorType.PERMISSION_DENIED,
    )
    assert m.error_type == ProbeErrorType.PERMISSION_DENIED
