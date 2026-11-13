"""
Tests for the v0.2 app-layer additions: gateway resolution policy,
JSON reports, monitor session incident transitions, route analysis
enrichment, check_target, and recent_rounds grouping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from dataclasses import dataclass as _dc
from datetime import UTC, datetime, timedelta

import pytest

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.app.gateway import resolve_gateway
from netscope.app.monitoring import MonitorSession, TransitionType
from netscope.app.reports import build_diagnose_report, serialize_diagnosis, serialize_measurement
from netscope.app.use_cases import (
    ROUTE_HISTORY_LIMIT,
    HistoryRound,
    check_target,
    observe_measurements,
    recent_rounds,
    run_route_analysis,
)
from netscope.core.baseline import UserBaseline
from netscope.core.models import (
    Diagnosis,
    Evidence,
    ExperienceEvent,
    ExperienceLevel,
    Hypothesis,
    ProbeType,
    RawMeasurement,
    RouteHop,
    RouteSnapshot,
    Severity,
)
from netscope.persistence.measurement_repository import MeasurementRepository


@_dc
class _FakeProbe:
    probe_type: ProbeType
    latency_ms: float = 10.0
    success: bool = True
    calls: list[str] = field(default_factory=list)

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        return RawMeasurement(probe_type=self.probe_type, target=target, success=self.success, latency_ms=self.latency_ms)


def _fake_container(**probe_kwargs) -> tuple[Container, dict[ProbeType, _FakeProbe]]:
    """Keys are probe-type names ('icmp', 'traceroute', ...); values are
    _FakeProbe field overrides. Returns (container, probes-by-type)."""
    probes: dict[ProbeType, _FakeProbe] = {}
    for probe_name, kwargs in probe_kwargs.items():
        probe_type = ProbeType(probe_name)
        probes[probe_type] = _FakeProbe(probe_type=probe_type, **kwargs)
    registry = ProbeRegistry(probes=probes)
    return Container(probe_registry=registry), probes


# ---------------------------------------------------------------------------
# Gateway resolution policy
# ---------------------------------------------------------------------------


def test_resolve_gateway_explicit_argument_wins():
    config = NetScopeConfig(gateway="10.0.0.138")
    assert resolve_gateway("192.168.1.254", config) == "192.168.1.254"


def test_resolve_gateway_none_sentinel_means_no_gateway():
    config = NetScopeConfig(gateway="10.0.0.138")
    assert resolve_gateway("none", config) is None


def test_resolve_gateway_auto_falls_back_to_config_then_none():
    config = NetScopeConfig(gateway="10.0.0.138")
    assert resolve_gateway("auto", config) == "10.0.0.138"
    # With no config gateway and no OS detection possible here, the
    # detector runs; in this sandbox it may or may not find one -- the
    # contract is "never raises, may be None".
    result = resolve_gateway("auto", NetScopeConfig())
    assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# Reports (JSON wire format)
# ---------------------------------------------------------------------------


def test_serialize_measurement_is_json_safe():
    measurement = RawMeasurement(
        probe_type=ProbeType.DNS,
        target="example.com",
        success=True,
        latency_ms=12.3,
        extra={"answers": ["93.184.216.34"], "nested": {"ok": True}},
    )
    data = serialize_measurement(measurement)
    assert json.loads(json.dumps(data))["extra"]["answers"] == ["93.184.216.34"]


def test_serialize_measurement_handles_non_primitive_extra():
    snapshot = RouteSnapshot(target="example.com")
    measurement = RawMeasurement(probe_type=ProbeType.TRACEROUTE, target="example.com", success=True, extra={"route": snapshot})
    data = serialize_measurement(measurement)
    # The RouteSnapshot object is reported as a type note, never a crash.
    assert data["extra"]["route"].startswith("<RouteSnapshot>")


def test_serialize_diagnosis_none_passthrough():
    assert serialize_diagnosis(None) is None


def test_serialize_diagnosis_full_shape():
    diagnosis = Diagnosis(
        classification=Hypothesis.ISP_ACCESS_ISSUE,
        evidence=[Evidence(metric="dns_latency", tested=True, observed_value=900.0, expected_value=20.0, deviation=6.0, severity=Severity.CRITICAL)],
        confidence=0.8,
        ruled_out=[Hypothesis.LOCAL_NETWORK_ISSUE],
    )
    data = serialize_diagnosis(diagnosis)
    assert data["classification"] == "isp_access_issue"
    assert data["evidence"][0]["severity"] == "critical"
    assert data["ruled_out"] == [{"hypothesis": "local_network_issue"}]
    assert json.loads(json.dumps(data))["confidence"] == 0.8


def test_build_diagnose_report_shape():
    measurements = [RawMeasurement(probe_type=ProbeType.ICMP, target="1.1.1.1", success=True, latency_ms=9.0)]
    experience = ExperienceEvent(timestamp=measurements[0].timestamp, score=90.0, level=ExperienceLevel.EXCELLENT)
    report = build_diagnose_report(measurements, experience, None, gateway="192.168.1.1")
    assert report["tool"] == "netscope"
    assert report["report"] == "diagnose"
    assert report["gateway"] == "192.168.1.1"
    assert report["experience"]["score"] == 90.0
    assert report["diagnosis"] is None
    assert len(report["measurements"]) == 1


# ---------------------------------------------------------------------------
# MonitorSession (incident transitions)
# ---------------------------------------------------------------------------


def _problem_diagnosis(classification: Hypothesis = Hypothesis.ISP_ACCESS_ISSUE) -> Diagnosis:
    return Diagnosis(
        classification=classification,
        evidence=[Evidence(metric="dns_latency", tested=True, severity=Severity.CRITICAL)],
        confidence=0.9,
    )


def _at(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def test_monitor_session_requires_sustained_problem_to_open():
    session = MonitorSession()
    # Round 1: problem (below threshold -> nothing)
    t1 = session.process(_problem_diagnosis(), _at(1))
    assert t1.kind is TransitionType.NONE
    # Round 2: healthy blip (resets the run -> still nothing)
    t2 = session.process(None, _at(2))
    assert t2.kind is TransitionType.NONE
    # Rounds 3-5: problem sustained -> opens on the 3rd consecutive
    t3 = session.process(_problem_diagnosis(), _at(3))
    assert t3.kind is TransitionType.NONE
    t4 = session.process(_problem_diagnosis(), _at(4))
    assert t4.kind is TransitionType.NONE
    t5 = session.process(_problem_diagnosis(), _at(5))
    assert t5.kind is TransitionType.OPENED
    assert t5.incident is not None
    assert t5.incident.is_active


def test_monitor_session_closes_after_sustained_healthy():
    session = MonitorSession()
    # Rounds 0-1: two problems, below the 3-round threshold -> nothing.
    assert session.process(_problem_diagnosis(), _at(0)).kind is TransitionType.NONE
    assert session.process(_problem_diagnosis(), _at(1)).kind is TransitionType.NONE
    # Round 2: third consecutive problem -> the incident opens.
    opened = session.process(_problem_diagnosis(), _at(2))
    assert opened.kind is TransitionType.OPENED
    for i in range(11, 14):
        closed = session.process(None, _at(i))
    assert closed.kind is TransitionType.CLOSED
    assert closed.incident is not None
    assert not closed.incident.is_active


def test_monitor_session_insufficient_evidence_never_flaps():
    session = MonitorSession()
    # Sustained problem -> opens exactly on the 3rd consecutive round.
    assert session.process(_problem_diagnosis(), _at(0)).kind is TransitionType.NONE
    assert session.process(_problem_diagnosis(), _at(1)).kind is TransitionType.NONE
    opened = session.process(_problem_diagnosis(), _at(2))
    assert opened.kind is TransitionType.OPENED
    session.process(_problem_diagnosis(), _at(3))  # stays open (updated)
    # A sustained stretch of INSUFFICIENT_EVIDENCE neither closes nor
    # re-opens anything -- "we don't know" is not resolution evidence.
    for i in range(11, 15):
        transition = session.process(Diagnosis(classification=Hypothesis.INSUFFICIENT_EVIDENCE, evidence=[], confidence=0.0), _at(i))
        assert transition.kind is TransitionType.NONE
    assert session.incident is not None and session.incident.is_active


# ---------------------------------------------------------------------------
# Route analysis use case (enrichment)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAsnLookup:
    mapping: dict = field(default_factory=dict)

    def lookup(self, ip_address: str):
        from netscope.core.asn import AsnInfo

        asn = self.mapping.get(ip_address)
        if asn is None:
            return None
        return AsnInfo(ip_address=ip_address, asn=asn, prefix=None)


@dataclass
class _FakeHostnameResolver:
    mapping: dict = field(default_factory=dict)

    def resolve_all(self, addresses):
        return {a: self.mapping.get(a) for a in addresses if a}


def _snapshot_with_hops():
    return RouteSnapshot(
        target="example.com",
        hops=[
            RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=1.0, packet_loss_pct=0.0),
            RouteHop(ttl=2, address="203.0.113.7", hostname=None, avg_rtt_ms=15.0, packet_loss_pct=0.0),
        ],
    )


def test_run_route_analysis_enriches_hops_and_analyzes_churn():
    container, probes = _fake_container(traceroute={})
    snapshot = _snapshot_with_hops()

    def run_with_snapshot(target, **options):
        probes[ProbeType.TRACEROUTE].calls.append(target)
        return RawMeasurement(probe_type=ProbeType.TRACEROUTE, target=target, success=True, extra={"route": snapshot})

    probes[ProbeType.TRACEROUTE].run = run_with_snapshot
    container.asn_lookup = _FakeAsnLookup(mapping={"203.0.113.7": 15169})
    container.hostname_resolver = _FakeHostnameResolver(mapping={"10.0.0.1": "router.local"})

    result = run_route_analysis("example.com", container)

    assert result.success
    assert result.snapshot is not None and result.churn is not None
    assert result.snapshot.hops[0].hostname == "router.local"
    assert result.snapshot.hops[1].asn == "AS15169"
    # Signature immunity: enrichment (asn/hostname) must not register as churn.
    assert result.churn.is_stable
    assert probes[ProbeType.TRACEROUTE].calls == ["example.com"]


def test_run_route_analysis_failed_traceroute_never_fabricates():
    container, probes = _fake_container(traceroute={"success": False})
    result = run_route_analysis("example.com", container)
    assert not result.success
    assert result.snapshot is None
    assert result.churn is None


def test_run_route_analysis_without_enrichment_tools():
    """No ASN lookup / resolver wired: hops keep their None fields, the
    pipeline still completes."""
    container, probes = _fake_container(traceroute={})
    snapshot = _snapshot_with_hops()

    def run_with_snapshot(target, **options):
        return RawMeasurement(probe_type=ProbeType.TRACEROUTE, target=target, success=True, extra={"route": snapshot})

    probes[ProbeType.TRACEROUTE].run = run_with_snapshot
    result = run_route_analysis("example.com", container, resolve_hostnames=False)
    assert result.success
    assert result.snapshot.hops[0].hostname is None
    assert result.snapshot.hops[0].asn is None


# ---------------------------------------------------------------------------
# Route memory (v0.8) -- churn across the STORED route history
# ---------------------------------------------------------------------------


class _FakeRouteRepository:
    """In-memory RouteRepository double: append-only save, load_recent
    oldest-first. Records its call order so tests can pin the
    compare-then-observe ordering the use case promises."""

    def __init__(self) -> None:
        self.snapshots: list[RouteSnapshot] = []
        self.calls: list[tuple[str, tuple, dict]] = []

    def save(self, snapshot: RouteSnapshot) -> None:
        self.calls.append(("save", (snapshot,), {}))
        self.snapshots.append(snapshot)

    def load_recent(self, target: str, limit: int = 20) -> list[RouteSnapshot]:
        self.calls.append(("load_recent", (target,), {"limit": limit}))
        if limit <= 0:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        matching = [snap for snap in self.snapshots if snap.target == target]
        return list(reversed(matching[-limit:]))


def _route_container_with_repo(repo: _FakeRouteRepository, addresses: list[str | None]) -> tuple[Container, _FakeRouteRepository]:
    """A container whose traceroute always returns the given path and
    whose route memory is the given fake."""
    probes: dict[ProbeType, _FakeProbe] = {}

    class _ScriptedTraceroute:
        probe_type = ProbeType.TRACEROUTE

        def run(self, target: str, **options: object) -> RawMeasurement:
            snapshot = RouteSnapshot(
                target=target,
                hops=[RouteHop(ttl=i + 1, address=addr, hostname=None, avg_rtt_ms=10.0, packet_loss_pct=0.0) for i, addr in enumerate(addresses)],
            )
            return RawMeasurement(probe_type=ProbeType.TRACEROUTE, target=target, success=True, extra={"route": snapshot})

    probes[ProbeType.TRACEROUTE] = _ScriptedTraceroute()  # type: ignore[assignment]
    container = Container(probe_registry=ProbeRegistry(probes=probes), route_repository=repo)
    return container, repo


def test_route_memory_first_observation_prior_runs_zero_and_snapshot_stored():
    repo = _FakeRouteRepository()
    container, repo = _route_container_with_repo(repo, ["10.0.0.1", "203.0.113.7"])

    result = run_route_analysis("example.com", container)

    assert result.success
    assert result.prior_runs == 0  # honest first-observation verdict, never "stable across 0 runs"
    assert result.churn is not None and result.churn.is_stable
    assert result.churn.snapshot_count == 1
    assert len(repo.snapshots) == 1  # the run itself got stored for the NEXT run


def test_route_memory_second_run_compares_against_the_stored_history():
    repo = _FakeRouteRepository()
    repo.save(
        RouteSnapshot(
            target="example.com",
            timestamp=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
            hops=[
                RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=9.0, packet_loss_pct=0.0),
                RouteHop(ttl=2, address="203.0.113.7", hostname=None, avg_rtt_ms=14.0, packet_loss_pct=0.0),
            ],
        )
    )
    container, repo = _route_container_with_repo(repo, ["10.0.0.1", "203.0.113.7"])

    result = run_route_analysis("example.com", container)

    assert result.prior_runs == 1
    assert result.churn is not None
    assert result.churn.snapshot_count == 2
    assert result.churn.is_stable  # same path across runs -- now actually MEANING something
    assert result.churn.last_change is None
    assert result.churn.window_start is not None


def test_route_memory_change_across_runs_carries_the_hop_diff():
    repo = _FakeRouteRepository()
    repo.save(
        RouteSnapshot(
            target="example.com",
            timestamp=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
            hops=[
                RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=9.0, packet_loss_pct=0.0),
                RouteHop(ttl=2, address="203.0.113.7", hostname=None, avg_rtt_ms=14.0, packet_loss_pct=0.0),
            ],
        )
    )
    container, repo = _route_container_with_repo(repo, ["10.0.0.1", "198.51.100.9"])

    result = run_route_analysis("example.com", container)

    assert result.prior_runs == 1
    assert result.churn is not None and not result.churn.is_stable
    change = result.churn.last_change
    assert change is not None
    assert [(h.ttl, h.previous_address, h.new_address) for h in change.changed_hops] == [(2, "203.0.113.7", "198.51.100.9")]


def test_route_memory_compare_then_observe_load_precedes_save():
    """The run's own snapshot must never appear in the window it is
    compared against -- load the stored history FIRST, analyze, THEN
    append (the same ordering doctrine v0.4 set for throughput
    baselines)."""
    repo = _FakeRouteRepository()
    container, repo = _route_container_with_repo(repo, ["10.0.0.1"])

    result = run_route_analysis("example.com", container)
    first_run_calls = [name for name, _, _ in repo.calls]

    assert first_run_calls == ["load_recent", "save"]  # load BEFORE save, exactly once each
    assert result.prior_runs == 0

    run_route_analysis("example.com", container)
    second_run_calls = [name for name, _, _ in repo.calls[2:]]

    assert second_run_calls == ["load_recent", "save"]
    assert len(repo.snapshots) == 2


def test_route_memory_respects_the_history_window_limit():
    repo = _FakeRouteRepository()
    container, repo = _route_container_with_repo(repo, ["10.0.0.1"])

    run_route_analysis("example.com", container)

    load_calls = [params for name, params, _ in repo.calls if name == "load_recent"]
    assert load_calls and load_calls[0] == ("example.com",)
    load_kwargs = [kwargs for name, _, kwargs in repo.calls if name == "load_recent"]
    assert load_kwargs and load_kwargs[0] == {"limit": ROUTE_HISTORY_LIMIT}


def test_route_memory_scoped_per_target():
    repo = _FakeRouteRepository()
    repo.save(
        RouteSnapshot(
            target="other.example.com",
            timestamp=datetime(2026, 11, 10, 7, 0, tzinfo=UTC),
            hops=[RouteHop(ttl=1, address="192.0.2.1", hostname=None, avg_rtt_ms=8.0, packet_loss_pct=0.0)],
        )
    )
    container, repo = _route_container_with_repo(repo, ["10.0.0.1"])

    result = run_route_analysis("example.com", container)

    # Another target's stored path is not this target's history.
    assert result.prior_runs == 0
    assert len(repo.snapshots) == 2  # the pre-seeded one plus this run's


def test_route_memory_without_repository_stays_v07_single_snapshot():
    """No RouteRepository wired: v0.7's behavior, byte for byte -- the
    analysis runs over the one in-memory snapshot, nothing is stored
    (an automatic observation is not a user-issued save)."""
    container, probes = _fake_container(traceroute={})
    snapshot = _snapshot_with_hops()

    def run_with_snapshot(target, **options):
        return RawMeasurement(probe_type=ProbeType.TRACEROUTE, target=target, success=True, extra={"route": snapshot})

    probes[ProbeType.TRACEROUTE].run = run_with_snapshot
    result = run_route_analysis("example.com", container)

    assert result.success
    assert result.prior_runs == 0
    assert result.churn is not None and result.churn.is_stable
    assert result.churn.snapshot_count == 1


# ---------------------------------------------------------------------------
# check_target
# ---------------------------------------------------------------------------


def test_check_target_runs_only_requested_checks():
    container, probes = _fake_container(icmp={}, dns={}, tcp={}, tls={}, http={})
    measurements, experience, diagnosis = check_target("example.com", container, {ProbeType.ICMP, ProbeType.TCP})

    assert {m.probe_type for m in measurements} == {ProbeType.ICMP, ProbeType.TCP}
    assert probes[ProbeType.ICMP].calls == ["example.com"]
    assert probes[ProbeType.DNS].calls == []
    assert experience.level in ExperienceLevel


def test_check_target_failed_check_becomes_critical_diagnosis():
    container, _ = _fake_container(icmp={}, http={"success": False})
    measurements, experience, diagnosis = check_target("example.com", container, {ProbeType.HTTP})
    assert diagnosis is not None
    assert diagnosis.classification == Hypothesis.SERVICE_ISSUE


# ---------------------------------------------------------------------------
# observe_measurements + recent_rounds
# ---------------------------------------------------------------------------


def test_observe_measurements_skips_failures_and_nones():
    baseline = UserBaseline()
    measurements = [
        RawMeasurement(probe_type=ProbeType.ICMP, target="ok", success=True, latency_ms=10.0),
        RawMeasurement(probe_type=ProbeType.ICMP, target="failed", success=False, latency_ms=None),
        RawMeasurement(probe_type=ProbeType.HTTP, target="no-loss-field", success=True, latency_ms=20.0, packet_loss_pct=None),
    ]
    observe_measurements(baseline, measurements)
    assert set(baseline.latency) == {"ok", "no-loss-field"}
    assert baseline.packet_loss == {}  # no packet-loss readings observed


def test_observe_measurements_observes_jitter_only_when_reported():
    """v0.13: the third store fills only from sweeps that actually
    report a variation reading -- a probe type without one (http) and
    a failed sweep leave it untouched, never poisoned with artifacts."""
    baseline = UserBaseline()
    measurements = [
        RawMeasurement(probe_type=ProbeType.ICMP, target="gw", success=True, latency_ms=10.0, jitter_ms=2.0),
        RawMeasurement(probe_type=ProbeType.HTTP, target="web", success=True, latency_ms=20.0, jitter_ms=None),
        RawMeasurement(probe_type=ProbeType.ICMP, target="failed", success=False, latency_ms=None, jitter_ms=None),
    ]
    observe_measurements(baseline, measurements)
    assert set(baseline.jitter) == {"gw"}
    assert baseline.jitter["gw"].count == 1
    assert baseline.jitter["gw"].mean == pytest.approx(2.0)


def test_recent_rounds_groups_measurements_by_time_window(tmp_path):
    repo = MeasurementRepository.open(tmp_path / "db.sqlite")
    container = Container(probe_registry=ProbeRegistry(), measurement_repository=repo)

    base = datetime.now(UTC)
    round_one = [base + timedelta(seconds=i) for i in range(4)]
    round_two = [base + timedelta(minutes=10, seconds=i) for i in range(3)]
    for ts in round_one + round_two:
        repo.save(RawMeasurement(probe_type=ProbeType.ICMP, target=f"t-{ts.second}", success=True, latency_ms=5.0, timestamp=ts))

    rounds = recent_rounds(container, limit=10, max_gap_seconds=120)
    assert len(rounds) == 2
    assert all(isinstance(r, HistoryRound) for r in rounds)
    assert len(rounds[0].measurements) == 4
    assert len(rounds[1].measurements) == 3
    assert rounds[0].experience is not None and rounds[1].experience is not None
    repo.close()


def test_recent_rounds_without_repository_returns_empty():
    container = Container(probe_registry=ProbeRegistry())
    assert recent_rounds(container) == []
