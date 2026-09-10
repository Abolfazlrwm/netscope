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

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.container import Container
from netscope.app.use_cases import diagnose_service, diagnose_service_with_connectivity, run_service_checks
from netscope.core.baseline import UserBaseline
from netscope.core.models import Diagnosis, Evidence, Hypothesis, ProbeType, RawMeasurement, Service, Severity


@dataclass
class _FakeProbe:
    """A minimal Probe-conforming fake: records every target it was
    called with, and returns a scripted RawMeasurement (or a scripted
    sequence of them across repeated calls)."""

    probe_type: ProbeType
    latency_ms: float = 10.0
    success: bool = True
    calls: list[str] = field(default_factory=list)

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        return RawMeasurement(probe_type=self.probe_type, target=target, success=self.success, latency_ms=self.latency_ms)


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


# ---------------------------------------------------------------------------
# Dependency boundaries
# ---------------------------------------------------------------------------


def _imports_of(module) -> tuple[set[str], set[str]]:
    tree = ast.parse(open(module.__file__).read())
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
    assert top_level <= {"__future__", "typing", "netscope"}
    for path in full_paths:
        if path in ("__future__", "typing"):
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
