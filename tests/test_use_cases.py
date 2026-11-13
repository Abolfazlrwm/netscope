"""
Tests for netscope.app.use_cases (TASK-033/034).

Per future-roadmap.md's TASK-033 row: "Unit tests using fake adapters
implementing core.ports.Probe." TASK-034 adds: "Unit tests for the
comparison logic with fake Diagnosis inputs" -- diagnose_service_with_
connectivity is a pure function over two Diagnosis objects, so its
tests construct Diagnosis/Evidence directly rather than going through
fake probes at all.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

import pytest

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.app.use_cases import (
    check_service,
    check_target,
    diagnose_service,
    diagnose_service_with_connectivity,
    list_services,
    remove_service,
    run_measurement_round,
    run_service_checks,
    run_speed_test,
    save_service,
)
from netscope.core.baseline import UserBaseline
from netscope.core.models import (
    Diagnosis,
    Evidence,
    ExperienceLevel,
    Hypothesis,
    ProbeErrorType,
    ProbeType,
    RawMeasurement,
    Service,
    Severity,
)
from netscope.persistence.baseline_repository import BaselineRepository


@dataclass
class _FakeProbe:
    """A minimal Probe-conforming fake: records every target it was
    called with, and returns a scripted RawMeasurement (or a scripted
    sequence of them across repeated calls)."""

    probe_type: ProbeType
    latency_ms: float = 10.0
    jitter_ms: float | None = None
    success: bool = True
    calls: list[str] = field(default_factory=list)

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        return RawMeasurement(
            probe_type=self.probe_type,
            target=target,
            success=self.success,
            latency_ms=self.latency_ms,
            jitter_ms=self.jitter_ms,
        )


def _mature_baseline(target: str, values: list[float]) -> UserBaseline:
    ub = UserBaseline()
    for v in values:
        ub.observe_latency(target, v)
    return ub


# ---------------------------------------------------------------------------
# Service configuration
# ---------------------------------------------------------------------------


def test_empty_enabled_checks_runs_nothing():
    icmp = _FakeProbe(ProbeType.ICMP)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))
    service = Service(name="svc", host="example.com")
    results = run_service_checks(service, container)
    assert results == []
    assert icmp.calls == []


def test_one_enabled_check_runs_exactly_that_check():
    icmp = _FakeProbe(ProbeType.ICMP)
    http = _FakeProbe(ProbeType.HTTP)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp, ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.ICMP})

    run_service_checks(service, container)

    assert icmp.calls == ["example.com"]
    assert http.calls == []


def test_multiple_enabled_checks_all_run():
    icmp = _FakeProbe(ProbeType.ICMP)
    dns = _FakeProbe(ProbeType.DNS)
    http = _FakeProbe(ProbeType.HTTP)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp, ProbeType.DNS: dns, ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.ICMP, ProbeType.DNS})

    run_service_checks(service, container)

    assert icmp.calls == ["example.com"]
    assert dns.calls == ["example.com"]
    assert http.calls == []  # not enabled -- must not run


def test_exact_configured_checks_are_respected_not_a_superset_or_subset():
    probes = {t: _FakeProbe(t) for t in (ProbeType.ICMP, ProbeType.DNS, ProbeType.TCP, ProbeType.TLS, ProbeType.HTTP)}
    container = Container(probe_registry=ProbeRegistry(probes=probes))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.TCP, ProbeType.TLS})

    results = run_service_checks(service, container)

    called_types = {m.probe_type for m in results}
    assert called_types == {ProbeType.TCP, ProbeType.TLS}
    for probe_type, probe in probes.items():
        expected_called = probe_type in {ProbeType.TCP, ProbeType.TLS}
        assert (len(probe.calls) == 1) == expected_called


# ---------------------------------------------------------------------------
# Execution behavior
# ---------------------------------------------------------------------------


def test_correct_target_host_is_passed_to_every_enabled_probe():
    icmp = _FakeProbe(ProbeType.ICMP)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))
    service = Service(name="svc", host="my-service.example.net", enabled_checks={ProbeType.ICMP})

    run_service_checks(service, container)

    assert icmp.calls == ["my-service.example.net"]


def test_run_service_checks_reuses_the_containers_probe_registry():
    """No new probe logic is created -- the adapter that runs is
    whatever is registered in container.probe_registry, looked up by
    ProbeType, exactly as any other measurement round would."""
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=42.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.ICMP})

    (result,) = run_service_checks(service, container)

    assert result.latency_ms == 42.0
    assert result.probe_type == ProbeType.ICMP


# ---------------------------------------------------------------------------
# Failure behavior
# ---------------------------------------------------------------------------


def test_failed_check_remains_represented_as_a_failure():
    http = _FakeProbe(ProbeType.HTTP, success=False)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})

    (result,) = run_service_checks(service, container)

    assert result.success is False


def test_failed_check_is_not_silently_converted_to_success():
    http = _FakeProbe(ProbeType.HTTP, success=False)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})

    diagnosis = diagnose_service(service, container)

    assert diagnosis is not None
    assert diagnosis.classification == Hypothesis.SERVICE_ISSUE


def test_one_failed_check_does_not_corrupt_an_unrelated_healthy_checks_result():
    icmp = _FakeProbe(ProbeType.ICMP, success=True, latency_ms=10.0)
    http = _FakeProbe(ProbeType.HTTP, success=False)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp, ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.ICMP, ProbeType.HTTP})

    results = run_service_checks(service, container)

    icmp_result = next(r for r in results if r.probe_type == ProbeType.ICMP)
    http_result = next(r for r in results if r.probe_type == ProbeType.HTTP)
    assert icmp_result.success is True
    assert icmp_result.latency_ms == 10.0
    assert http_result.success is False


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_different_services_configurations_do_not_leak_into_each_other():
    icmp = _FakeProbe(ProbeType.ICMP)
    http = _FakeProbe(ProbeType.HTTP)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp, ProbeType.HTTP: http}))

    service_a = Service(name="A", host="a.example.com", enabled_checks={ProbeType.ICMP})
    service_b = Service(name="B", host="b.example.com", enabled_checks={ProbeType.HTTP})

    run_service_checks(service_a, container)
    run_service_checks(service_b, container)

    assert icmp.calls == ["a.example.com"]
    assert http.calls == ["b.example.com"]


def test_mutating_one_services_enabled_checks_does_not_affect_another():
    service_a = Service(name="A", host="a.example.com", enabled_checks={ProbeType.ICMP})
    service_b = Service(name="B", host="b.example.com", enabled_checks=set(service_a.enabled_checks))

    service_a.enabled_checks.add(ProbeType.HTTP)

    assert service_b.enabled_checks == {ProbeType.ICMP}


# ---------------------------------------------------------------------------
# diagnose_service -- aggregation into a per-service Diagnosis
# ---------------------------------------------------------------------------


def test_diagnose_service_with_no_enabled_checks_is_insufficient_evidence():
    container = Container(probe_registry=ProbeRegistry(probes={}))
    service = Service(name="svc", host="example.com")

    diagnosis = diagnose_service(service, container)

    assert diagnosis is not None
    assert diagnosis.classification == Hypothesis.INSUFFICIENT_EVIDENCE
    assert diagnosis.confidence == 0.0


def test_diagnose_service_all_checks_healthy_against_mature_baseline_is_none():
    http = _FakeProbe(ProbeType.HTTP, latency_ms=20.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})
    baseline = _mature_baseline("example.com", [20.0, 19.0, 21.0, 20.0, 20.0, 20.0])

    diagnosis = diagnose_service(service, container, baseline=baseline)

    assert diagnosis is None


def test_diagnose_service_anomalous_check_against_mature_baseline_is_service_issue():
    http = _FakeProbe(ProbeType.HTTP, latency_ms=900.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})
    baseline = _mature_baseline("example.com", [20.0, 19.0, 21.0, 20.0, 20.0, 20.0])

    diagnosis = diagnose_service(service, container, baseline=baseline)

    assert diagnosis is not None
    assert diagnosis.classification == Hypothesis.SERVICE_ISSUE


def test_diagnose_service_defaults_to_a_fresh_baseline_when_none_given():
    """No BaselineRepository-backed persistence exists yet (matches
    ui/cli.py's own documented status quo) -- diagnose_service must
    still work standalone, without requiring a caller to construct a
    UserBaseline."""
    http = _FakeProbe(ProbeType.HTTP, latency_ms=20.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})

    diagnosis = diagnose_service(service, container)  # no baseline argument

    assert diagnosis is None  # a single successful, untested-baseline check is not a problem


def test_diagnose_service_never_mutates_the_baseline_it_is_given():
    http = _FakeProbe(ProbeType.HTTP, latency_ms=999.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})
    baseline = _mature_baseline("example.com", [20.0] * 6)

    targets_before = set(baseline.latency.keys())
    count_before = baseline.latency["example.com"].count

    diagnose_service(service, container, baseline=baseline)

    assert set(baseline.latency.keys()) == targets_before
    assert baseline.latency["example.com"].count == count_before


def test_diagnose_service_evidence_metric_names_are_scoped_to_service_and_categorize_correctly():
    """The 'service_' metric prefix this function uses is core/diagnosis
    .py's own existing SERVICE_ISSUE category (established in TASK-026,
    unchanged here) -- this pins that the two stay in sync."""
    http = _FakeProbe(ProbeType.HTTP, latency_ms=900.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})
    baseline = _mature_baseline("example.com", [20.0] * 6)

    diagnosis = diagnose_service(service, container, baseline=baseline)

    assert all(e.metric.startswith("service_") for e in diagnosis.evidence)


def test_diagnose_service_includes_the_jitter_twin_for_each_check():
    """v0.13: every latency Evidence now has a `_jitter` sibling. For a
    check type that never reports variation (http), that sibling is
    tested-but-zero-confidence -- present, never a fabricated claim."""
    http = _FakeProbe(ProbeType.HTTP, latency_ms=900.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})
    baseline = _mature_baseline("example.com", [20.0] * 6)

    diagnosis = diagnose_service(service, container, baseline=baseline)

    assert diagnosis is not None  # the 900ms latency reading is CRITICAL against the mature baseline
    metrics = {e.metric for e in diagnosis.evidence}
    assert metrics == {"service_http_latency", "service_http_jitter"}
    jitter_evidence = next(e for e in diagnosis.evidence if e.metric == "service_http_jitter")
    assert jitter_evidence.tested is True
    assert jitter_evidence.confidence == 0.0


def test_diagnose_service_anomalous_jitter_against_mature_jitter_baseline_is_service_issue():
    """A steady 20ms line that suddenly wobbles at 40ms against a ~2ms
    history is a diagnosable problem even though latency and loss look
    perfect -- the v0.13 headline, expressed through the diagnosis."""
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=20.0, jitter_ms=40.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.ICMP})
    baseline = _mature_baseline("example.com", [20.0] * 6)
    for v in [2.0, 2.2, 1.8, 2.1, 2.0]:
        baseline.observe_jitter("example.com", v)

    diagnosis = diagnose_service(service, container, baseline=baseline)

    assert diagnosis is not None
    assert diagnosis.classification == Hypothesis.SERVICE_ISSUE


# ---------------------------------------------------------------------------
# Dependency boundaries
# ---------------------------------------------------------------------------


def _imports_of(module) -> tuple[set[str], set[str]]:
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    top_level, full_paths = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level.add(alias.name.split(".")[0])
                full_paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
            full_paths.add(node.module)
    return top_level, full_paths


def test_use_cases_module_does_not_import_ui():
    import netscope.app.use_cases as m

    _, full_paths = _imports_of(m)
    assert "netscope.ui" not in full_paths


def test_use_cases_module_does_not_contain_concrete_network_implementation():
    """app orchestrates; it must not import sqlite3, socket-level, or
    HTTP client libraries directly -- probe execution stays inside
    adapters/probes/*, reached only via ProbeRegistry."""
    import netscope.app.use_cases as m

    _, full_paths = _imports_of(m)
    for forbidden in ("sqlite3", "socket", "httpx", "dns", "icmplib", "ssl"):
        assert forbidden not in full_paths


def test_use_cases_module_only_imports_netscope_app_and_core():
    import netscope.app.use_cases as m

    top_level, full_paths = _imports_of(m)
    # v0.2 adds `dataclasses`/`datetime` (stdlib) for the RouteAnalysisResult/
    # HistoryRound dataclasses and round grouping -- the netscope-level
    # invariant (app + core only, never adapters/persistence/ui) is unchanged.
    assert top_level <= {"__future__", "typing", "dataclasses", "datetime", "netscope"}
    for path in full_paths:
        if path in ("__future__", "typing", "dataclasses", "datetime"):
            continue
        assert path.startswith("netscope.app.") or path.startswith("netscope.core.")


def test_core_diagnosis_does_not_import_app():
    """Dependency direction stays one-way: app -> core, never core -> app."""
    import netscope.core.diagnosis as m

    _, full_paths = _imports_of(m)
    assert not any(p.startswith("netscope.app") for p in full_paths)


def test_core_models_does_not_import_app():
    import netscope.core.models as m

    _, full_paths = _imports_of(m)
    assert not any(p.startswith("netscope.app") for p in full_paths)


# ===========================================================================
# TASK-034 -- diagnose_service_with_connectivity (pure comparison logic)
# ===========================================================================


def _problem_diagnosis(classification=Hypothesis.SERVICE_ISSUE, metric="m", confidence=0.9) -> Diagnosis:
    return Diagnosis(
        classification=classification,
        evidence=[Evidence(metric=metric, tested=True, observed_value=900.0, expected_value=20.0, deviation=6.0, severity=Severity.CRITICAL)],
        confidence=confidence,
    )


def _insufficient_diagnosis(metric="m") -> Diagnosis:
    """Matches diagnose()'s own real output shape for INSUFFICIENT_EVIDENCE:
    confidence is always 0.0, never fabricated."""
    return Diagnosis(
        classification=Hypothesis.INSUFFICIENT_EVIDENCE,
        evidence=[Evidence(metric=metric, tested=False)],
        confidence=0.0,
    )


# --- Case A: service healthy + connectivity healthy ------------------------


def test_both_healthy_produces_no_diagnosis():
    assert diagnose_service_with_connectivity(None, None) is None


# --- Case B: service unhealthy + connectivity healthy -----------------------


def test_service_unhealthy_connectivity_healthy_is_service_specific():
    service_diag = _problem_diagnosis(Hypothesis.SERVICE_ISSUE, metric="service_http_latency")
    result = diagnose_service_with_connectivity(service_diag, None)

    assert result is not None
    assert result.classification == Hypothesis.SERVICE_ISSUE
    assert result.confidence == service_diag.confidence
    assert result.evidence == service_diag.evidence


def test_service_unhealthy_connectivity_healthy_rules_out_general_connectivity():
    service_diag = _problem_diagnosis(Hypothesis.SERVICE_ISSUE)
    result = diagnose_service_with_connectivity(service_diag, None)
    assert Hypothesis.GENERAL_CONNECTIVITY_ISSUE in result.ruled_out


def test_service_unhealthy_connectivity_healthy_does_not_duplicate_an_existing_ruled_out_entry():
    service_diag = Diagnosis(
        classification=Hypothesis.SERVICE_ISSUE,
        evidence=[Evidence(metric="service_http_latency", severity=Severity.CRITICAL)],
        confidence=0.9,
        ruled_out=[Hypothesis.GENERAL_CONNECTIVITY_ISSUE],
    )
    result = diagnose_service_with_connectivity(service_diag, None)
    assert result.ruled_out.count(Hypothesis.GENERAL_CONNECTIVITY_ISSUE) == 1


# --- Case C: service unhealthy + connectivity unhealthy ---------------------


def test_both_unhealthy_attributes_to_general_connectivity_not_the_service():
    """'Do NOT incorrectly blame the service alone' -- a confirmed
    general problem is the more fundamental explanation."""
    service_diag = _problem_diagnosis(Hypothesis.SERVICE_ISSUE, metric="service_http_latency")
    general_diag = _problem_diagnosis(Hypothesis.ISP_ACCESS_ISSUE, metric="dns_latency")

    result = diagnose_service_with_connectivity(service_diag, general_diag)

    assert result.classification == Hypothesis.ISP_ACCESS_ISSUE
    assert result.confidence == general_diag.confidence


def test_both_unhealthy_merges_evidence_from_both_diagnoses():
    service_diag = _problem_diagnosis(Hypothesis.SERVICE_ISSUE, metric="service_http_latency")
    general_diag = _problem_diagnosis(Hypothesis.ISP_ACCESS_ISSUE, metric="dns_latency")

    result = diagnose_service_with_connectivity(service_diag, general_diag)

    metrics = {e.metric for e in result.evidence}
    assert "service_http_latency" in metrics
    assert "dns_latency" in metrics


# --- Case D: service healthy + connectivity unhealthy -----------------------


def test_service_healthy_connectivity_unhealthy_surfaces_the_general_problem():
    general_diag = _problem_diagnosis(Hypothesis.LOCAL_NETWORK_ISSUE, metric="gateway_latency")
    result = diagnose_service_with_connectivity(None, general_diag)

    assert result is not None
    assert result.classification == Hypothesis.LOCAL_NETWORK_ISSUE  # never reattributed as SERVICE_ISSUE


def test_service_healthy_connectivity_unhealthy_does_not_fabricate_a_service_failure():
    general_diag = _problem_diagnosis(Hypothesis.LOCAL_NETWORK_ISSUE)
    result = diagnose_service_with_connectivity(None, general_diag)
    assert result.classification != Hypothesis.SERVICE_ISSUE


# --- Case E: insufficient evidence -------------------------------------------


def test_service_insufficient_connectivity_healthy_preserves_service_uncertainty():
    service_diag = _insufficient_diagnosis()
    result = diagnose_service_with_connectivity(service_diag, None)
    assert result is service_diag
    assert result.classification == Hypothesis.INSUFFICIENT_EVIDENCE


def test_service_healthy_connectivity_insufficient_still_confirms_service_is_fine():
    general_diag = _insufficient_diagnosis()
    result = diagnose_service_with_connectivity(None, general_diag)
    assert result is None


def test_both_insufficient_stays_insufficient():
    service_diag = _insufficient_diagnosis(metric="service_http_latency")
    general_diag = _insufficient_diagnosis(metric="dns_latency")
    result = diagnose_service_with_connectivity(service_diag, general_diag)
    assert result.classification == Hypothesis.INSUFFICIENT_EVIDENCE
    assert result.confidence == 0.0


def test_service_unhealthy_connectivity_insufficient_does_not_overconfidently_blame_service():
    """THE KEY case: mirrors the untested-gateway fix one layer up. A
    clear-looking service problem must not be confidently classified as
    SERVICE_ISSUE when general connectivity's own status couldn't be
    confirmed -- an unconfirmed alternative explanation blocks full
    confidence in the specific one."""
    service_diag = _problem_diagnosis(Hypothesis.SERVICE_ISSUE, metric="service_http_latency", confidence=0.95)
    general_diag = _insufficient_diagnosis(metric="dns_latency")

    result = diagnose_service_with_connectivity(service_diag, general_diag)

    assert result.classification == Hypothesis.INSUFFICIENT_EVIDENCE
    assert result.confidence == 0.0
    assert result.classification != Hypothesis.SERVICE_ISSUE  # never fake 100%/high confidence from incomplete data


def test_service_unhealthy_connectivity_insufficient_still_preserves_both_evidence_sets():
    service_diag = _problem_diagnosis(Hypothesis.SERVICE_ISSUE, metric="service_http_latency")
    general_diag = _insufficient_diagnosis(metric="dns_latency")

    result = diagnose_service_with_connectivity(service_diag, general_diag)

    metrics = {e.metric for e in result.evidence}
    assert "service_http_latency" in metrics
    assert "dns_latency" in metrics


# --- Determinism / no mutation of inputs -------------------------------------


def test_comparison_does_not_mutate_the_input_diagnoses():
    service_diag = _problem_diagnosis(Hypothesis.SERVICE_ISSUE)
    general_diag = _problem_diagnosis(Hypothesis.ISP_ACCESS_ISSUE, metric="dns_latency")
    service_ruled_out_before = list(service_diag.ruled_out)
    general_evidence_before = list(general_diag.evidence)

    diagnose_service_with_connectivity(service_diag, general_diag)

    assert service_diag.ruled_out == service_ruled_out_before
    assert general_diag.evidence == general_evidence_before


def test_comparison_result_is_a_real_diagnosis_object_not_a_dict_or_boolean():
    result = diagnose_service_with_connectivity(_problem_diagnosis(), None)
    assert isinstance(result, Diagnosis)


# ---------------------------------------------------------------------------
# End-to-end: diagnose_service() feeding into the comparison
# ---------------------------------------------------------------------------


def test_diagnose_service_output_can_feed_directly_into_the_comparison():
    """Confirms the two TASK-033/034 functions compose without any glue
    -- diagnose_service already produces exactly the Diagnosis shape
    diagnose_service_with_connectivity expects."""
    http = _FakeProbe(ProbeType.HTTP, latency_ms=900.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})
    baseline = _mature_baseline("example.com", [20.0] * 6)

    service_diagnosis = diagnose_service(service, container, baseline=baseline)
    result = diagnose_service_with_connectivity(service_diagnosis, None)

    assert result is not None
    assert result.classification == Hypothesis.SERVICE_ISSUE


def test_task_033_apis_remain_backward_compatible():
    """run_service_checks and diagnose_service keep their exact TASK-033
    signatures/behavior -- TASK-034 only adds a new function alongside
    them."""
    http = _FakeProbe(ProbeType.HTTP, latency_ms=20.0)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))
    service = Service(name="svc", host="example.com", enabled_checks={ProbeType.HTTP})

    measurements = run_service_checks(service, container)
    assert len(measurements) == 1

    diagnosis = diagnose_service(service, container)
    assert diagnosis is None  # single successful, untested-baseline check is still not a problem


# ---------------------------------------------------------------------------
# Dependency boundaries (re-verified for the new function)
# ---------------------------------------------------------------------------


def test_comparison_function_does_not_import_probes_or_container():
    """diagnose_service_with_connectivity is pure -- it must not need
    Container/ProbeRegistry at all, unlike run_service_checks/
    diagnose_service in the same file."""
    import inspect

    source = inspect.getsource(diagnose_service_with_connectivity)
    assert "container" not in source.lower()
    assert "probe_registry" not in source.lower()


# ===========================================================================
# TASK-035 -- run_measurement_round
# ===========================================================================


def _round_container(icmp=None, dns=None, http=None) -> Container:
    probes = {}
    probes[ProbeType.ICMP] = icmp if icmp is not None else _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    probes[ProbeType.DNS] = dns if dns is not None else _FakeProbe(ProbeType.DNS, latency_ms=10.0)
    probes[ProbeType.HTTP] = http if http is not None else _FakeProbe(ProbeType.HTTP, latency_ms=10.0)
    return Container(probe_registry=ProbeRegistry(probes=probes))


def test_round_without_a_gateway_does_not_probe_a_gateway():
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    container = _round_container(icmp=icmp)
    config = NetScopeConfig()

    measurements, _, _ = run_measurement_round(container, config)

    assert icmp.calls == [config.public_dns_target]  # only the public DNS ICMP target, no gateway
    assert len(measurements) == 3  # public_dns (icmp) + dns_lookup (dns) + public_cdn (http)


def test_round_with_an_explicit_gateway_probes_it_first():
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    container = _round_container(icmp=icmp)
    config = NetScopeConfig()

    measurements, _, _ = run_measurement_round(container, config, gateway="192.168.1.1")

    assert icmp.calls == ["192.168.1.1", config.public_dns_target]
    assert len(measurements) == 4


def test_round_falls_back_to_configs_default_gateway():
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    container = _round_container(icmp=icmp)
    config = NetScopeConfig(gateway="10.0.0.1")

    run_measurement_round(container, config)  # no gateway= argument given

    assert icmp.calls[0] == "10.0.0.1"


def test_round_explicit_gateway_argument_overrides_config_default():
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    container = _round_container(icmp=icmp)
    config = NetScopeConfig(gateway="10.0.0.1")

    run_measurement_round(container, config, gateway="192.168.1.1")

    assert icmp.calls[0] == "192.168.1.1"


def test_round_probes_configured_targets_not_hardcoded_ones():
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    dns = _FakeProbe(ProbeType.DNS, latency_ms=10.0)
    http = _FakeProbe(ProbeType.HTTP, latency_ms=10.0)
    container = _round_container(icmp=icmp, dns=dns, http=http)
    config = NetScopeConfig(public_dns_target="9.9.9.9", dns_lookup_domain="custom.example", public_cdn_url="https://custom-cdn.example/")

    run_measurement_round(container, config)

    assert icmp.calls == ["9.9.9.9"]
    assert dns.calls == ["custom.example"]
    assert http.calls == ["https://custom-cdn.example/"]


def test_round_returns_an_experience_event():
    container = _round_container()
    config = NetScopeConfig()

    _, experience, _ = run_measurement_round(container, config)

    assert experience.level in (ExperienceLevel.EXCELLENT, ExperienceLevel.GOOD, ExperienceLevel.DEGRADED, ExperienceLevel.POOR, ExperienceLevel.DOWN)
    assert 0.0 <= experience.score <= 100.0


def test_round_all_healthy_but_gateway_untested_is_insufficient_evidence():
    """Preserves the pre-TASK-035 CLI's exact observable behavior: with
    no gateway supplied, the round cannot confirm the local hop, so the
    diagnosis is INSUFFICIENT_EVIDENCE, not a fabricated healthy
    result."""
    container = _round_container()
    config = NetScopeConfig()

    _, _, diagnosis = run_measurement_round(container, config)

    assert diagnosis is not None
    assert diagnosis.classification == Hypothesis.INSUFFICIENT_EVIDENCE


def test_round_with_gateway_and_mature_baseline_all_healthy_is_none():
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    dns = _FakeProbe(ProbeType.DNS, latency_ms=10.0)
    http = _FakeProbe(ProbeType.HTTP, latency_ms=10.0)
    container = _round_container(icmp=icmp, dns=dns, http=http)
    config = NetScopeConfig()

    baseline = UserBaseline()
    for target in ("192.168.1.1", config.public_dns_target, config.public_cdn_url):
        for v in [10.0] * 6:
            baseline.observe_latency(target, v)

    _, _, diagnosis = run_measurement_round(container, config, gateway="192.168.1.1", baseline=baseline)

    assert diagnosis is None


def test_round_observes_successful_readings_into_the_baseline():
    """v0.2 contract (replaces the pre-0.2 read-only contract): a round
    judges itself against the baseline it was handed, THEN observes its
    successful readings into it -- so the next round is judged against
    slightly more history. Failed probes are never observed: a failed
    probe has no latency to learn from."""
    container = _round_container()
    config = NetScopeConfig()
    baseline = UserBaseline()
    for v in [10.0] * 6:
        baseline.observe_latency(config.public_dns_target, v)
    count_before = baseline.latency[config.public_dns_target].count

    run_measurement_round(container, config, baseline=baseline)

    # The DNS ICMP target is a _FakeProbe success with a latency: observed.
    assert baseline.latency[config.public_dns_target].count == count_before + 1
    # The HTTP target also succeeded and now exists in the baseline.
    assert config.public_cdn_url in baseline.latency
    # DNS lookup (a successful DNS probe) too.
    assert config.dns_lookup_domain in baseline.latency


def test_round_does_not_observe_failed_probes_into_the_baseline():
    icmp = _FakeProbe(ProbeType.ICMP, latency_ms=10.0)
    failing_http = _FakeProbe(ProbeType.HTTP, latency_ms=None, success=False)
    container = _round_container(icmp=icmp, http=failing_http)
    config = NetScopeConfig()
    baseline = UserBaseline()

    run_measurement_round(container, config, baseline=baseline)

    # DNS ICMP succeeded (observed), the HTTP probe failed (not observed).
    assert config.public_dns_target in baseline.latency
    assert config.public_cdn_url not in baseline.latency


def test_round_persists_only_the_baseline_itself():
    """v0.2 persistence contract: the round persists the *baseline* via
    the container's BaselineRepository (that is the whole point of
    learned baselines outliving a process), but still never persists
    measurements itself -- that stays the caller's job."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(run_measurement_round))
    body_without_docstring = tree.body[0].body[1:]  # skip the docstring Expr node
    code_only = ast.unparse(ast.Module(body=body_without_docstring, type_ignores=[]))
    assert "MeasurementRepository" not in code_only
    # Exactly one .save() call, and it is the baseline repository's.
    assert code_only.count(".save(") == 1
    assert "baseline_repository" in code_only


# ---------------------------------------------------------------------------
# run_speed_test -- throughput baseline learning (v0.4)
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedBandwidthProbe:
    """Returns scripted throughput readings in order, repeating the
    last one once the script runs out (or a failure, if scripted)."""

    probe_type: ProbeType = ProbeType.BANDWIDTH
    mbps_script: list[float] = field(default_factory=lambda: [90.0])
    success: bool = True
    calls: list[str] = field(default_factory=list)

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        if not self.success:
            return RawMeasurement(
                probe_type=self.probe_type,
                target=target,
                success=False,
                error="HTTP 500",
                error_type=ProbeErrorType.HTTP_FAILURE,
            )
        i = min(len(self.calls) - 1, len(self.mbps_script) - 1)
        return RawMeasurement(
            probe_type=self.probe_type,
            target=target,
            success=True,
            extra={
                "throughput_mbps": self.mbps_script[i],
                "peak_mbps": self.mbps_script[i] + 5.0,
                "bytes_transferred": 1_000_000,
                "duration_s": 1.0,
                "sample_count": 2,
            },
        )


def _speed_container(tmp_path, probe: _ScriptedBandwidthProbe) -> Container:
    repo = BaselineRepository.open(tmp_path / "speed.db")
    return Container(probe_registry=ProbeRegistry(probes={ProbeType.BANDWIDTH: probe}), baseline_repository=repo)


def test_speed_test_compares_then_observes_into_the_throughput_baseline(tmp_path):
    """Three runs only LEARN (no verdict yet); the fourth is judged
    against the three BEFORE it -- baseline_samples says so."""
    probe = _ScriptedBandwidthProbe(mbps_script=[90.0, 90.0, 90.0])
    container = _speed_container(tmp_path, probe)
    config = NetScopeConfig()

    for _ in range(3):
        m = run_speed_test(container, config)
        assert "verdict" not in m.extra

    fourth = run_speed_test(container, config)
    assert fourth.extra["baseline_samples"] == 3
    assert fourth.extra["verdict"] == "as_usual"
    assert fourth.extra["baseline_mean_mbps"] == pytest.approx(90.0)
    assert fourth.extra["deviation_pct"] == pytest.approx(0.0)


def test_speed_test_reports_slower_than_usual(tmp_path):
    probe = _ScriptedBandwidthProbe(mbps_script=[90.0, 90.0, 90.0, 50.0])
    container = _speed_container(tmp_path, probe)
    config = NetScopeConfig()

    for _ in range(3):
        run_speed_test(container, config)

    slow = run_speed_test(container, config)
    assert slow.extra["verdict"] == "slower_than_usual"
    assert slow.extra["deviation_pct"] < -15.0


def test_speed_test_never_touches_the_latency_or_loss_baseline(tmp_path):
    """The v0.3 doctrine holds under the v0.4 learning: five speed
    tests fill ONLY the throughput store."""
    probe = _ScriptedBandwidthProbe()
    container = _speed_container(tmp_path, probe)
    config = NetScopeConfig()

    for _ in range(5):
        run_speed_test(container, config)

    loaded = container.baseline_repository.load()
    assert loaded.latency == {}
    assert loaded.packet_loss == {}
    assert loaded.throughput[config.bandwidth_url].count == 5


def test_failed_speed_test_is_never_observed(tmp_path):
    probe = _ScriptedBandwidthProbe(success=False)
    container = _speed_container(tmp_path, probe)

    m = run_speed_test(container, NetScopeConfig())

    assert m.success is False
    assert container.baseline_repository.load().throughput == {}


# ---------------------------------------------------------------------------
# Service persistence (v0.7) -- TASK-032's storage half
# ---------------------------------------------------------------------------


class _FakeServiceRepository:
    """In-memory stand-in for the ServiceRepository port: upsert by
    name, alphabetical load_all, remove reports existence, get for the
    created/updated distinction -- the documented contract, nothing
    more."""

    def __init__(self) -> None:
        self._services: dict[str, Service] = {}

    def save(self, service: Service) -> None:
        self._services[service.name] = service

    def load_all(self) -> list[Service]:
        return [self._services[name] for name in sorted(self._services)]

    def get(self, name: str) -> Service | None:
        return self._services.get(name)

    def remove(self, name: str) -> bool:
        return self._services.pop(name, None) is not None


def test_save_service_persists_and_reports_created():
    repo = _FakeServiceRepository()
    container = Container(probe_registry=ProbeRegistry(), service_repository=repo)

    svc, created = save_service("My VPN", "vpn.example.net", {ProbeType.ICMP, ProbeType.HTTP}, container)

    assert created is True
    assert isinstance(svc, Service)
    assert repo.load_all() == [svc]
    assert repo.load_all()[0].enabled_checks == {ProbeType.ICMP, ProbeType.HTTP}


def test_save_service_upsert_reports_updated_not_created():
    repo = _FakeServiceRepository()
    container = Container(probe_registry=ProbeRegistry(), service_repository=repo)
    save_service("My VPN", "old.example.net", {ProbeType.ICMP}, container)

    svc, created = save_service("My VPN", "new.example.net", {ProbeType.TLS}, container)

    assert created is False
    assert svc.host == "new.example.net"
    loaded = repo.load_all()
    assert len(loaded) == 1
    assert loaded[0].host == "new.example.net"


def test_save_service_rejects_an_empty_name():
    container = Container(probe_registry=ProbeRegistry(), service_repository=_FakeServiceRepository())
    with pytest.raises(ValueError, match="name"):
        save_service("   ", "vpn.example.net", {ProbeType.ICMP}, container)


def test_save_service_rejects_an_empty_host():
    container = Container(probe_registry=ProbeRegistry(), service_repository=_FakeServiceRepository())
    with pytest.raises(ValueError, match="host"):
        save_service("My VPN", "", {ProbeType.ICMP}, container)


def test_save_service_rejects_invalid_checks_with_the_domain_message():
    container = Container(probe_registry=ProbeRegistry(), service_repository=_FakeServiceRepository())
    with pytest.raises(ValueError, match="enabled_checks"):
        save_service("My VPN", "vpn.example.net", {ProbeType.TRACEROUTE}, container)


def test_save_service_without_a_repository_raises_instead_of_silently_dropping():
    container = Container(probe_registry=ProbeRegistry(), service_repository=None)
    with pytest.raises(RuntimeError, match="ServiceRepository"):
        save_service("My VPN", "vpn.example.net", {ProbeType.ICMP}, container)


def test_remove_service_reports_whether_it_existed():
    repo = _FakeServiceRepository()
    container = Container(probe_registry=ProbeRegistry(), service_repository=repo)
    save_service("My VPN", "vpn.example.net", {ProbeType.ICMP}, container)

    assert remove_service("My VPN", container) is True
    assert remove_service("My VPN", container) is False
    assert remove_service("never saved", container) is False


def test_remove_service_without_a_repository_raises():
    container = Container(probe_registry=ProbeRegistry(), service_repository=None)
    with pytest.raises(RuntimeError, match="ServiceRepository"):
        remove_service("My VPN", container)


def test_list_services_returns_the_curated_list_alphabetically():
    repo = _FakeServiceRepository()
    container = Container(probe_registry=ProbeRegistry(), service_repository=repo)
    save_service("zebra panel", "z.example.net", {ProbeType.ICMP}, container)
    save_service("alpha cdn", "a.example.net", {ProbeType.HTTP}, container)

    names = [svc.name for svc in list_services(container)]
    assert names == ["alpha cdn", "zebra panel"]


def test_list_services_without_a_repository_is_an_empty_list():
    container = Container(probe_registry=ProbeRegistry(), service_repository=None)
    assert list_services(container) == []


def test_check_service_runs_exactly_the_service_s_own_checks():
    icmp = _FakeProbe(ProbeType.ICMP)
    http = _FakeProbe(ProbeType.HTTP)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp, ProbeType.HTTP: http}))
    service = Service(name="My VPN", host="vpn.example.net", enabled_checks={ProbeType.ICMP})

    measurements, experience, diagnosis = check_service(service, container)

    assert icmp.calls == ["vpn.example.net"]
    assert http.calls == []  # a check the service didn't enable never runs
    assert all(isinstance(m, RawMeasurement) for m in measurements)
    assert experience is not None
    # A fully healthy check set is diagnose()'s own "confirmed healthy"
    # convention: None, never a fabricated all-clear Diagnosis object.
    assert diagnosis is None


def test_check_target_and_check_service_agree_on_the_same_pipeline():
    """The unsaved and saved forms share one pipeline by construction:
    identical inputs produce identical measurements, score and
    diagnosis."""
    icmp = _FakeProbe(ProbeType.ICMP)
    container_a = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))
    container_b = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: _FakeProbe(ProbeType.ICMP)}))
    baseline = _mature_baseline("vpn.example.net", [10.0, 11.0, 12.0])

    target_results = check_target("vpn.example.net", container_a, {ProbeType.ICMP}, baseline)
    service_results = check_service(
        Service(name="vpn.example.net", host="vpn.example.net", enabled_checks={ProbeType.ICMP}),
        container_b,
        baseline,
    )

    # RawMeasurement carries its own utcnow() stamp, so compare the
    # measured substance, not the wall clock.
    assert [(m.probe_type, m.target, m.success, m.latency_ms) for m in target_results[0]] == [
        (m.probe_type, m.target, m.success, m.latency_ms) for m in service_results[0]
    ]
    assert target_results[1].score == service_results[1].score


# ---------------------------------------------------------------------------
# Packet-loss confirmation (v0.9): a partial-loss sweep is a suspicion,
# re-swept before it reaches scoring, the baseline or the diagnosis.
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedProbe:
    """A Probe fake returning a scripted sequence of measurements across
    repeated `run` calls -- the last script entry repeats for any call
    beyond the script's length."""

    probe_type: ProbeType
    script: list[RawMeasurement]
    calls: list[str] = field(default_factory=list)

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        return self.script[min(len(self.calls), len(self.script)) - 1]


def _lossy(target: str, loss: float | None, *, success: bool = True, latency: float | None = 10.0) -> RawMeasurement:
    return RawMeasurement(
        probe_type=ProbeType.ICMP,
        target=target,
        success=success,
        latency_ms=latency if success else None,
        packet_loss_pct=loss,
    )


def test_partial_loss_sweep_is_reconfirmed_and_merged():
    icmp = _ScriptedProbe(ProbeType.ICMP, script=[_lossy("1.1.1.1", 50.0), _lossy("1.1.1.1", 0.0), _lossy("1.1.1.1", 25.0)])
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))

    [m] = run_service_checks(Service(name="dns", host="1.1.1.1", enabled_checks={ProbeType.ICMP}), container)

    assert icmp.calls == ["1.1.1.1"] * 3  # original + two confirmation sweeps
    assert m.packet_loss_pct == pytest.approx(25.0)  # the window average
    assert m.extra["loss_confirmed"] is True
    assert m.extra["loss_rounds"] == [50.0, 0.0, 25.0]


def test_transient_loss_reports_not_confirmed_when_confirmations_come_back_clean():
    icmp = _ScriptedProbe(ProbeType.ICMP, script=[_lossy("1.1.1.1", 25.0), _lossy("1.1.1.1", 0.0), _lossy("1.1.1.1", 0.0)])
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))

    [m] = run_service_checks(Service(name="dns", host="1.1.1.1", enabled_checks={ProbeType.ICMP}), container)

    assert icmp.calls == ["1.1.1.1"] * 3
    assert m.success
    assert m.extra["loss_confirmed"] is False


def test_clean_sweep_still_runs_exactly_once():
    icmp = _ScriptedProbe(ProbeType.ICMP, script=[_lossy("1.1.1.1", 0.0)])
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))

    [m] = run_service_checks(Service(name="dns", host="1.1.1.1", enabled_checks={ProbeType.ICMP}), container)

    assert icmp.calls == ["1.1.1.1"]
    assert "loss_rounds" not in m.extra


def test_dead_target_still_runs_exactly_once():
    icmp = _ScriptedProbe(ProbeType.ICMP, script=[_lossy("1.1.1.1", None, success=False, latency=None)])
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp}))

    [m] = run_service_checks(Service(name="dns", host="1.1.1.1", enabled_checks={ProbeType.ICMP}), container)

    assert icmp.calls == ["1.1.1.1"]
    assert not m.success


def test_loss_free_probes_pass_through_untouched():
    http = _ScriptedProbe(ProbeType.HTTP, script=[RawMeasurement(probe_type=ProbeType.HTTP, target="https://x/", success=True, latency_ms=50.0)])
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.HTTP: http}))

    [m] = run_service_checks(Service(name="web", host="https://x/", enabled_checks={ProbeType.HTTP}), container)

    assert http.calls == ["https://x/"]
    assert m.extra == {}


def test_measurement_round_confirms_gateway_loss_before_scoring():
    """The gateway's partial-loss sweep gets confirmed and the *merged*
    window -- not the raw first sweep -- is what scoring, the baseline
    and the returned measurements see."""
    # Script order = call order: the gateway's sweep + its two
    # confirmations, then the round's public-DNS sweep (same ICMP
    # adapter, clean) -- the last entry repeats, so that's enough.
    icmp = _ScriptedProbe(
        ProbeType.ICMP,
        script=[
            _lossy("192.168.1.1", 25.0),
            _lossy("192.168.1.1", 0.0),
            _lossy("192.168.1.1", 0.0),
            _lossy("1.1.1.1", 0.0),
        ],
    )
    http = _FakeProbe(ProbeType.HTTP)
    dns = _FakeProbe(ProbeType.DNS)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.ICMP: icmp, ProbeType.HTTP: http, ProbeType.DNS: dns}))

    measurements, _experience, _diagnosis = run_measurement_round(
        container,
        NetScopeConfig(gateway="192.168.1.1"),
        gateway="192.168.1.1",
        baseline=UserBaseline(),
    )

    gateway_readings = [m for m in measurements if m.target == "192.168.1.1"]
    assert len(gateway_readings) == 1  # merged window, not three raw sweeps
    merged = gateway_readings[0]
    assert merged.extra["loss_confirmed"] is False
    assert merged.extra["loss_rounds"] == [25.0, 0.0, 0.0]
