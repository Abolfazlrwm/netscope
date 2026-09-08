"""
Characterization tests for netscope.core.diagnosis (TASK-026).

These replace tests/test_diagnosis.py's previous characterization of
netscope.diagnosis.engine, which used fixed static thresholds
(latency > 250ms, packet loss > 5%) and did NOT consult a baseline.
That model, and its static-threshold boundary tests, are retired here
on purpose (architecture-overview.md SS11; TASK-025's precedent of
retiring static-threshold tests when the model they characterize is
replaced) -- core/diagnosis.py never looks at a raw latency/loss number
or a threshold at all, only at already-classified Evidence.severity.

Most importantly, this file replaces
`test_untested_gateway_currently_behaves_as_healthy`, which
*intentionally* documented a bug (an untested gateway being treated as
confirmed-healthy, letting a diagnosis confidently claim "Upstream ISP
issue" and even list "ruled out: Local network issue" despite the local
network never having been measured). That old assertion would now fail
outright against the corrected engine -- this file asserts the
corrected behavior instead: an untested gateway can never be ruled out,
and the result must honestly reflect the resulting uncertainty via
Hypothesis.INSUFFICIENT_EVIDENCE.

Fully offline and deterministic: Evidence objects are constructed by
hand, never produced from real probes or a real UserBaseline.
"""

from __future__ import annotations

import ast

import pytest

from netscope.core.baseline import UserBaseline
from netscope.core.diagnosis import diagnose, evidence_from_latency, evidence_from_packet_loss, evidence_from_route_churn
from netscope.core.models import Diagnosis, Evidence, Hypothesis, ProbeType, RawMeasurement, RouteHop, RouteSnapshot, Severity
from netscope.core.routing import analyze_route_churn


def _healthy(metric: str, observed=10.0, expected=10.0) -> Evidence:
    return Evidence(metric=metric, tested=True, observed_value=observed, expected_value=expected, deviation=0.0, severity=Severity.INFO)


def _bad(metric: str, severity=Severity.CRITICAL, observed=300.0, expected=20.0, deviation=6.0, confidence=1.0) -> Evidence:
    return Evidence(metric=metric, tested=True, observed_value=observed, expected_value=expected, deviation=deviation, severity=severity, confidence=confidence)


def _untested(metric: str) -> Evidence:
    return Evidence(metric=metric, tested=False)


# ---------------------------------------------------------------------------
# 1. Healthy local + healthy public connectivity
# ---------------------------------------------------------------------------


def test_all_healthy_tested_evidence_produces_no_diagnosis():
    """Diagnosis conceptually represents a diagnosed *problem*
    (module-boundaries.md) -- Hypothesis has no "no issue" member on
    purpose (architecture-overview.md SS11), so a confirmed-healthy
    result is `None`, not a Diagnosis object."""
    result = diagnose([_healthy("gateway_latency"), _healthy("dns_latency"), _healthy("destination_latency")])
    assert result is None


# ---------------------------------------------------------------------------
# 2. Local gateway failure (+ public connectivity failure too)
# ---------------------------------------------------------------------------


def test_bad_gateway_is_diagnosed_as_local_network_issue():
    d = diagnose([_bad("gateway_latency"), _healthy("dns_latency"), _healthy("destination_latency")])
    assert d.classification == Hypothesis.LOCAL_NETWORK_ISSUE


def test_bad_gateway_takes_priority_even_if_everything_else_is_also_bad():
    """Preserved concept from the old suite (module-boundaries.md /
    implementation-audit.md: the three-way localization *concept* is
    correct and should survive the rewrite) -- if the local hop itself
    demonstrably fails, that's diagnosed regardless of what upstream
    targets also show, since a broken local link can make everything
    past it look bad too."""
    d = diagnose([_bad("gateway_latency"), _bad("dns_latency"), _bad("destination_latency")])
    assert d.classification == Hypothesis.LOCAL_NETWORK_ISSUE


# ---------------------------------------------------------------------------
# 3. Gateway untested + public connectivity failure -- THE BUG FIX
# ---------------------------------------------------------------------------


def test_untested_gateway_with_upstream_failures_is_insufficient_evidence_not_healthy():
    """This is the corrected replacement for
    test_untested_gateway_currently_behaves_as_healthy. The old
    assertion (`"Upstream ISP issue" in d.likely_cause` with
    `"Local network issue"` listed as ruled out) would now fail: an
    untested gateway must never let the engine confidently blame
    upstream, and must never appear as ruled out."""
    d = diagnose([_untested("gateway_latency"), _bad("dns_latency"), _bad("destination_latency")])

    assert d is not None
    assert d.classification == Hypothesis.INSUFFICIENT_EVIDENCE
    assert Hypothesis.LOCAL_NETWORK_ISSUE not in d.ruled_out
    assert not any(isinstance(item, Evidence) and item.metric == "gateway_latency" for item in d.ruled_out)


def test_untested_gateway_evidence_is_still_visible_on_the_diagnosis():
    """The untested gateway must not be silently dropped either --
    it should be visible in Diagnosis.evidence so the explanation layer
    can honestly say "gateway: not tested"."""
    gateway_evidence = _untested("gateway_latency")
    d = diagnose([gateway_evidence, _bad("dns_latency")])
    assert gateway_evidence in d.evidence


# ---------------------------------------------------------------------------
# 4. Gateway absent entirely (not even present as untested Evidence)
# ---------------------------------------------------------------------------


def test_no_gateway_evidence_at_all_does_not_fabricate_a_gateway_verdict():
    """Distinct from "untested": here there is no gateway-category
    Evidence item in the list whatsoever (e.g. a caller that never
    probes the gateway at all). The engine must not invent one -- with
    everything actually present healthy, this is indistinguishable from
    case 1."""
    result = diagnose([_healthy("dns_latency"), _healthy("destination_latency")])
    assert result is None


def test_no_gateway_evidence_with_a_real_upstream_problem_still_attributes_upstream():
    """No gateway evidence exists to contradict an upstream attribution
    (unlike case 3, where an untested-but-present gateway item
    specifically blocks ruling it out) -- this can be safely diagnosed."""
    d = diagnose([_bad("dns_latency"), _bad("destination_latency")])
    assert d.classification == Hypothesis.ISP_ACCESS_ISSUE


# ---------------------------------------------------------------------------
# 5. DNS failure with relevant other connectivity evidence
# ---------------------------------------------------------------------------


def test_gateway_and_destination_healthy_only_dns_bad_is_dns_issue():
    d = diagnose([_healthy("gateway_latency"), _bad("dns_latency"), _healthy("destination_latency")])
    assert d.classification == Hypothesis.DNS_ISSUE
    assert Hypothesis.LOCAL_NETWORK_ISSUE in d.ruled_out


# ---------------------------------------------------------------------------
# 6. Public connectivity (destination) failure
# ---------------------------------------------------------------------------


def test_gateway_and_dns_healthy_only_destination_bad_is_destination_issue():
    d = diagnose([_healthy("gateway_latency"), _healthy("dns_latency"), _bad("destination_latency")])
    assert d.classification == Hypothesis.DESTINATION_ISSUE
    assert Hypothesis.LOCAL_NETWORK_ISSUE in d.ruled_out


def test_gateway_healthy_dns_and_destination_both_bad_is_isp_issue():
    """Multiple independent upstream categories failing together points
    beyond any one of them -- preserved from the old suite's "Upstream
    ISP issue" concept."""
    d = diagnose([_healthy("gateway_latency"), _bad("dns_latency"), _bad("destination_latency")])
    assert d.classification == Hypothesis.ISP_ACCESS_ISSUE
    assert Hypothesis.LOCAL_NETWORK_ISSUE in d.ruled_out


def test_routing_evidence_alone_is_routing_degradation():
    d = diagnose([_healthy("gateway_latency"), _bad("route_churn"), _healthy("dns_latency")])
    assert d.classification == Hypothesis.ROUTING_DEGRADATION


# ---------------------------------------------------------------------------
# 7. Missing measurements -- unknown/absent must not become success
# ---------------------------------------------------------------------------


def test_all_evidence_untested_is_insufficient_evidence():
    d = diagnose([_untested("gateway_latency"), _untested("dns_latency"), _untested("destination_latency")])
    assert d.classification == Hypothesis.INSUFFICIENT_EVIDENCE
    assert d.confidence == 0.0


def test_empty_evidence_list_is_insufficient_evidence_not_healthy():
    d = diagnose([])
    assert d is not None
    assert d.classification == Hypothesis.INSUFFICIENT_EVIDENCE
    assert d.confidence == 0.0


def test_healthy_gateway_with_untested_dns_and_destination_is_insufficient_not_healthy():
    """Symmetric case to the untested-gateway bug: an untested upstream
    target must not silently read as healthy just because the gateway
    itself is confirmed fine."""
    d = diagnose([_healthy("gateway_latency"), _untested("dns_latency"), _untested("destination_latency")])
    assert d is not None
    assert d.classification == Hypothesis.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# 8. Conflicting evidence -- deterministic, no contradictory certainty
# ---------------------------------------------------------------------------


def test_conflicting_evidence_for_the_same_metric_does_not_produce_contradictory_certainty():
    """Two Evidence items for the same metric disagree (one healthy, one
    critical). The engine must pick one deterministic outcome, not
    simultaneously claim the gateway is both fine and the cause."""
    d = diagnose([_healthy("gateway_latency"), _bad("gateway_latency"), _healthy("dns_latency")])
    assert d.classification == Hypothesis.LOCAL_NETWORK_ISSUE  # a demonstrated problem is not overridden by a conflicting healthy reading
    assert Hypothesis.LOCAL_NETWORK_ISSUE not in d.ruled_out  # and it is certainly not also "ruled out"


# ---------------------------------------------------------------------------
# 9. Evidence insufficiency represents uncertainty per the domain model
# ---------------------------------------------------------------------------


def test_insufficient_evidence_diagnosis_has_zero_confidence_never_fabricated():
    d = diagnose([_untested("gateway_latency"), _bad("dns_latency")])
    assert d.confidence == 0.0  # "Never return fake 100% confidence from missing data" -- and never any nonzero confidence either, here


# ---------------------------------------------------------------------------
# 10. Confidence calculation from evidence quality
# ---------------------------------------------------------------------------


def test_confidence_reflects_the_supporting_evidence_own_confidence():
    weak = diagnose([_bad("gateway_latency", confidence=0.4), _healthy("dns_latency")])
    strong = diagnose([_bad("gateway_latency", confidence=1.0), _healthy("dns_latency")])
    assert weak.confidence < strong.confidence


def test_confidence_is_reduced_when_some_relevant_evidence_is_untested():
    """Same problem evidence, but one run also has an untested item
    diluting how complete the overall picture is -- confidence must be
    lower, not identical."""
    complete = diagnose([_healthy("gateway_latency"), _bad("dns_latency"), _healthy("destination_latency")])
    incomplete = diagnose([_untested("gateway_latency"), _bad("dns_latency"), _healthy("destination_latency")])
    # incomplete resolves to INSUFFICIENT_EVIDENCE (confidence forced 0.0);
    # this asserts that is strictly lower than the complete, confident case.
    assert incomplete.confidence < complete.confidence


# ---------------------------------------------------------------------------
# Diagnosis contains the original Evidence list
# ---------------------------------------------------------------------------


def test_diagnosis_evidence_field_contains_every_input_evidence_item():
    inputs = [_bad("gateway_latency"), _healthy("dns_latency"), _healthy("destination_latency")]
    d = diagnose(inputs)
    assert d.evidence == inputs


# ---------------------------------------------------------------------------
# Regression coverage carried over conceptually from the old suite
# ---------------------------------------------------------------------------


def test_unreachable_gateway_reading_is_still_a_local_network_issue():
    """A failed (not just slow) gateway probe is still evidence of a
    local problem, regardless of how the caller chose to represent
    "unreachable" as severity -- CRITICAL, same as high latency."""
    d = diagnose([_bad("gateway_latency", severity=Severity.CRITICAL), _healthy("dns_latency"), _healthy("destination_latency")])
    assert d.classification == Hypothesis.LOCAL_NETWORK_ISSUE


def test_uncategorized_metric_name_is_general_connectivity_issue_not_dropped():
    """A metric name with no recognized prefix (this task's own brief
    uses the bare example metric="latency") still surfaces as a real,
    reported problem -- just not one this module can localize -- rather
    than being silently ignored."""
    d = diagnose([_bad("latency")])
    assert d.classification == Hypothesis.GENERAL_CONNECTIVITY_ISSUE
    assert d.evidence != []


# ---------------------------------------------------------------------------
# TASK-027 -- Evidence generation
# ---------------------------------------------------------------------------


def _mature_latency_baseline(target: str, values: list[float]) -> UserBaseline:
    ub = UserBaseline()
    for v in values:
        ub.observe_latency(target, v)
    return ub


def _measurement(target="1.1.1.1", latency_ms=None, packet_loss_pct=None, success=True):
    return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=success, latency_ms=latency_ms, packet_loss_pct=packet_loss_pct)


# 1. Healthy/normal measurement evidence
def test_evidence_from_latency_mature_baseline_normal_value_is_info():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0, 19.0, 21.0, 20.0, 20.0, 20.0])
    e = evidence_from_latency("gateway_latency", _measurement(latency_ms=20.0), baseline)
    assert e.tested is True
    assert e.severity == Severity.INFO
    assert e.confidence == 1.0


# 2. Abnormal measurement relative to a mature baseline
def test_evidence_from_latency_mature_baseline_large_deviation_is_critical():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0, 19.0, 21.0, 20.0, 20.0, 20.0])
    e = evidence_from_latency("gateway_latency", _measurement(latency_ms=500.0), baseline)
    assert e.severity == Severity.CRITICAL
    assert e.deviation is not None and e.deviation > 0


# 3. Same measurement, different baselines -> different evidence
def test_evidence_from_latency_same_value_differs_by_baseline():
    low = _mature_latency_baseline("1.1.1.1", [10.0, 11.0, 9.0, 10.0, 10.0, 11.0])
    high = _mature_latency_baseline("1.1.1.1", [180.0, 190.0, 185.0, 195.0, 188.0, 192.0])
    probe = _measurement(latency_ms=100.0)
    assert evidence_from_latency("gateway_latency", probe, low).severity == Severity.CRITICAL
    assert evidence_from_latency("gateway_latency", probe, high).severity == Severity.INFO


# 4. Per-target baseline isolation
def test_evidence_from_latency_uses_only_the_matching_targets_baseline():
    ub = UserBaseline()
    for v in [10.0] * 6:
        ub.observe_latency("fast-target", v)
    for v in [200.0] * 6:
        ub.observe_latency("slow-target", v)

    fast_probe = evidence_from_latency("gateway_latency", _measurement(target="fast-target", latency_ms=200.0), ub)
    slow_probe = evidence_from_latency("gateway_latency", _measurement(target="slow-target", latency_ms=200.0), ub)
    assert fast_probe.severity == Severity.CRITICAL  # 200ms is way off "fast-target"
    assert slow_probe.severity == Severity.INFO  # but exactly "slow-target"'s own mean


# 5. Insufficient baseline history
def test_evidence_from_latency_insufficient_history_has_low_confidence_not_healthy_certainty():
    ub = UserBaseline()
    for v in [20.0, 20.0]:  # 2 samples, below the 5-sample maturity cutoff
        ub.observe_latency("1.1.1.1", v)
    e = evidence_from_latency("gateway_latency", _measurement(latency_ms=20.0), ub)
    assert e.confidence == pytest.approx(2 / 5)
    assert e.severity == Severity.INFO  # no claim of abnormality either, just low confidence


def test_evidence_from_latency_never_probed_target_has_zero_confidence():
    e = evidence_from_latency("gateway_latency", _measurement(target="never-seen", latency_ms=20.0), UserBaseline())
    assert e.confidence == 0.0


# 6. Zero-variance baseline behavior
def test_evidence_from_latency_zero_variance_exact_match_is_info():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0] * 6)
    e = evidence_from_latency("gateway_latency", _measurement(latency_ms=20.0), baseline)
    assert e.severity == Severity.INFO


def test_evidence_from_latency_zero_variance_any_deviation_is_critical():
    """deviation_sigma() returns 0.0 for a zero-stddev baseline
    regardless of how far value is from the mean -- this must not be
    trusted directly, or a first-ever deviation from a perfectly stable
    target would read as healthy (same TASK-025 precedent)."""
    baseline = _mature_latency_baseline("1.1.1.1", [20.0] * 6)
    assert baseline.latency_deviation_sigma("1.1.1.1", 999.0) == 0.0  # confirms the raw baseline behavior
    e = evidence_from_latency("gateway_latency", _measurement(latency_ms=999.0), baseline)
    assert e.severity == Severity.CRITICAL


# 7. Packet-loss evidence
def test_evidence_from_packet_loss_uses_the_packet_loss_baseline_not_latency():
    ub = UserBaseline()
    for v in [0.0] * 6:
        ub.observe_packet_loss("1.1.1.1", v)
    e = evidence_from_packet_loss("gateway_packet_loss", _measurement(packet_loss_pct=25.0), ub)
    assert e.severity == Severity.CRITICAL


# 8. Failed measurement evidence
def test_evidence_from_latency_failed_measurement_is_critical_without_consulting_baseline():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0] * 6)  # mature, but must not matter here
    e = evidence_from_latency("gateway_latency", _measurement(latency_ms=None, success=False), baseline)
    assert e.tested is True
    assert e.severity == Severity.CRITICAL
    assert e.observed_value is None  # no fabricated value derived from a failed probe


# 9. Untested/missing observation semantics
def test_evidence_from_latency_none_measurement_is_untested():
    e = evidence_from_latency("gateway_latency", None, UserBaseline())
    assert e.tested is False
    assert e.observed_value is None
    assert e.source is None


def test_evidence_from_packet_loss_none_measurement_is_untested():
    e = evidence_from_packet_loss("gateway_packet_loss", None, UserBaseline())
    assert e.tested is False


def test_evidence_from_route_churn_none_result_is_untested():
    e = evidence_from_route_churn("route_churn", None)
    assert e.tested is False
    assert e.observed_value is None


# 10. Multiple measurements / evidence generation feeding diagnose()
def test_evidence_generation_output_feeds_diagnose_end_to_end():
    baseline = _mature_latency_baseline("gw", [10.0] * 6)
    gateway = evidence_from_latency("gateway_latency", _measurement(target="gw", latency_ms=10.0), baseline)
    dns = evidence_from_latency("dns_latency", None, baseline)  # untested
    d = diagnose([gateway, dns])
    assert d is not None
    assert d.classification == Hypothesis.INSUFFICIENT_EVIDENCE  # untested dns must not read as healthy


# 11. Evidence source preservation
def test_evidence_from_latency_preserves_the_raw_measurement_as_source():
    baseline = _mature_latency_baseline("1.1.1.1", [20.0] * 6)
    m = _measurement(latency_ms=20.0)
    e = evidence_from_latency("gateway_latency", m, baseline)
    assert e.source is m


def test_evidence_from_route_churn_preserves_the_latest_snapshot_as_source():
    snap = RouteSnapshot(target="1.1.1.1", hops=[RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0)])
    churn = analyze_route_churn([snap, snap])
    e = evidence_from_route_churn("route_churn", churn, latest_snapshot=snap)
    assert e.source is snap


# 12/13. Confidence and severity behavior already covered throughout
# (mature/insufficient/zero-variance/never-probed cases above).


# 14. Route-related evidence
def test_evidence_from_route_churn_stable_route_is_info():
    snap = RouteSnapshot(target="1.1.1.1", hops=[RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0)])
    churn = analyze_route_churn([snap, snap])
    e = evidence_from_route_churn("route_churn", churn)
    assert e.severity == Severity.INFO


def test_evidence_from_route_churn_changed_route_is_at_least_warning():
    snap_a = RouteSnapshot(target="1.1.1.1", hops=[RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0)])
    snap_b = RouteSnapshot(target="1.1.1.1", hops=[RouteHop(ttl=1, address="10.0.0.2", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0)])
    churn = analyze_route_churn([snap_a, snap_b])
    e = evidence_from_route_churn("route_churn", churn, latest_snapshot=snap_b)
    assert e.severity in (Severity.WARNING, Severity.CRITICAL)
    assert e.deviation == 1.0


def test_route_churn_evidence_feeds_diagnose_as_routing_degradation():
    snap_a = RouteSnapshot(target="1.1.1.1", hops=[RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0)])
    snap_b = RouteSnapshot(target="1.1.1.1", hops=[RouteHop(ttl=1, address="10.0.0.2", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0)])
    churn = analyze_route_churn([snap_a, snap_b])
    route_evidence = evidence_from_route_churn("route_churn", churn, latest_snapshot=snap_b)
    baseline = _mature_latency_baseline("1.1.1.1", [10.0] * 6)
    gateway_evidence = evidence_from_latency("gateway_latency", _measurement(target="1.1.1.1", latency_ms=10.0), baseline)
    d = diagnose([gateway_evidence, route_evidence])
    assert d.classification == Hypothesis.ROUTING_DEGRADATION


# 15. Baseline is never mutated during evidence generation
def test_evidence_generation_never_mutates_the_baseline_it_is_given():
    ub = UserBaseline()
    for v in [20.0] * 6:
        ub.observe_latency("1.1.1.1", v)
    for v in [0.0] * 6:
        ub.observe_packet_loss("1.1.1.1", v)

    targets_before = set(ub.latency.keys())
    count_before = ub.latency["1.1.1.1"].count

    evidence_from_latency("gateway_latency", _measurement(target="1.1.1.1", latency_ms=999.0), ub)
    evidence_from_latency("dns_latency", _measurement(target="never-seen-before.example", latency_ms=5.0), ub)
    evidence_from_packet_loss("gateway_packet_loss", _measurement(target="1.1.1.1", packet_loss_pct=50.0), ub)

    assert set(ub.latency.keys()) == targets_before  # no new target entries created
    assert ub.latency["1.1.1.1"].count == count_before  # no new observations recorded


# ---------------------------------------------------------------------------
# Core dependency isolation
# ---------------------------------------------------------------------------


def test_diagnosis_module_only_imports_stdlib_and_netscope_core():
    """core/diagnosis.py must stay infrastructure-free: no adapters,
    persistence, UI, sqlite3, or network libraries. TASK-027 added
    netscope.core.baseline and netscope.core.routing alongside
    netscope.core.models (needed by the new evidence-generation
    functions) -- still only netscope.core.* siblings and the standard
    library, never leaving core."""
    import netscope.core.diagnosis as diagnosis_module

    tree = ast.parse(open(diagnosis_module.__file__).read())
    top_level_modules = set()
    full_module_paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_modules.add(alias.name.split(".")[0])
                full_module_paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_modules.add(node.module.split(".")[0])
            full_module_paths.add(node.module)

    assert top_level_modules <= {"__future__", "netscope"}
    assert full_module_paths <= {
        "__future__",
        "netscope.core.baseline",
        "netscope.core.models",
        "netscope.core.routing",
    }
    assert "netscope.core.scoring" not in full_module_paths  # independent consumers of baseline, not of each other
