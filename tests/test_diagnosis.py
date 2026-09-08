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

from netscope.core.diagnosis import diagnose
from netscope.core.models import Diagnosis, Evidence, Hypothesis, Severity


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
# Core dependency isolation
# ---------------------------------------------------------------------------


def test_diagnosis_module_only_imports_stdlib_and_core_models():
    """core/diagnosis.py must stay infrastructure-free: no adapters,
    persistence, UI, sqlite3, or network libraries -- only the standard
    library and netscope.core.models."""
    import netscope.core.diagnosis as diagnosis_module

    tree = ast.parse(open(diagnosis_module.__file__).read())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert modules <= {"__future__", "netscope"}
