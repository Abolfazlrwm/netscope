"""
Characterization tests for netscope.core.incidents (TASK-028).

Per module-boundaries.md's Monitoring section and its own stated
testing strategy: "Pure unit tests feeding a synthetic sequence of
Diagnosis results (e.g. '3 consecutive ISP_ACCESS_ISSUE diagnoses 5
minutes apart' -> Incident opens; '1 bad diagnosis surrounded by
healthy ones' -> no Incident) -- fully deterministic, no timing
dependency needed since timestamps are passed in, not read from the
wall clock, inside core."

Fully offline: every Diagnosis/Evidence/MonitoringRound below is
hand-constructed with explicit timestamps -- nothing reads the real
clock or touches the network.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone

from netscope.core.incidents import MonitoringRound, detect_incident
from netscope.core.models import Diagnosis, Evidence, Hypothesis, Incident, Severity

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ts(i: int) -> datetime:
    return _T0 + timedelta(minutes=5 * i)


def _evidence(metric="dns_latency", severity=Severity.CRITICAL) -> Evidence:
    return Evidence(metric=metric, tested=True, observed_value=200.0, expected_value=15.0, deviation=6.0, severity=severity)


def _diagnosis(i: int, classification=Hypothesis.ISP_ACCESS_ISSUE, evidence=None, confidence=0.8) -> Diagnosis:
    return Diagnosis(classification=classification, evidence=evidence or [_evidence()], confidence=confidence, timestamp=_ts(i))


def _healthy_round(i: int) -> MonitoringRound:
    return MonitoringRound(timestamp=_ts(i), diagnosis=None)


def _problem_round(i: int, classification=Hypothesis.ISP_ACCESS_ISSUE, evidence=None, confidence=0.8) -> MonitoringRound:
    return MonitoringRound(timestamp=_ts(i), diagnosis=_diagnosis(i, classification, evidence, confidence))


def _insufficient_round(i: int) -> MonitoringRound:
    return MonitoringRound(timestamp=_ts(i), diagnosis=_diagnosis(i, Hypothesis.INSUFFICIENT_EVIDENCE, evidence=[]))


# ---------------------------------------------------------------------------
# 1. No diagnosis does not fabricate an incident
# ---------------------------------------------------------------------------


def test_no_rounds_returns_existing_incident_unchanged():
    assert detect_incident([], target="1.1.1.1") is None


def test_all_healthy_rounds_produce_no_incident():
    rounds = [_healthy_round(i) for i in range(5)]
    assert detect_incident(rounds, target="1.1.1.1") is None


def test_single_bad_diagnosis_surrounded_by_healthy_ones_does_not_open_an_incident():
    """module-boundaries.md's own worked example."""
    rounds = [_healthy_round(0), _problem_round(1), _healthy_round(2)]
    assert detect_incident(rounds, target="1.1.1.1") is None


# ---------------------------------------------------------------------------
# 2. Insufficient evidence is not a confirmed incident
# ---------------------------------------------------------------------------


def test_sustained_insufficient_evidence_does_not_open_an_incident():
    rounds = [_insufficient_round(i) for i in range(3)]
    assert detect_incident(rounds, target="1.1.1.1") is None


def test_sustained_insufficient_evidence_does_not_close_an_active_incident():
    """A stretch of 'we don't know' is not positive evidence a real
    problem resolved -- must not be treated as equivalent to healthy."""
    opened = detect_incident([_problem_round(i) for i in range(3)], target="1.1.1.1")
    still_open = detect_incident([_insufficient_round(i) for i in range(3, 6)], existing_incident=opened, target="1.1.1.1")
    assert still_open is opened
    assert still_open.is_active


# ---------------------------------------------------------------------------
# 3. Confirmed problematic diagnosis produces an Incident
# ---------------------------------------------------------------------------


def test_three_consecutive_matching_diagnoses_opens_an_incident():
    """module-boundaries.md's own worked example, verbatim."""
    rounds = [_problem_round(i, Hypothesis.ISP_ACCESS_ISSUE) for i in range(3)]
    incident = detect_incident(rounds, target="1.1.1.1")
    assert incident is not None
    assert incident.is_active
    assert incident.diagnosis.classification == Hypothesis.ISP_ACCESS_ISSUE


def test_incident_started_at_is_the_first_round_of_the_sustained_run_not_the_latest():
    rounds = [_problem_round(i) for i in range(3)]
    incident = detect_incident(rounds, target="1.1.1.1")
    assert incident.started_at == _ts(0)


def test_fewer_than_threshold_consecutive_diagnoses_does_not_open_an_incident():
    rounds = [_problem_round(i) for i in range(2)]  # only 2, default threshold is 3
    assert detect_incident(rounds, target="1.1.1.1") is None


def test_sustain_threshold_is_configurable():
    rounds = [_problem_round(i) for i in range(2)]
    incident = detect_incident(rounds, target="1.1.1.1", sustain_threshold=2)
    assert incident is not None


# ---------------------------------------------------------------------------
# 4. Canonical model reuse -- no duplicate Diagnosis/Incident fields
# ---------------------------------------------------------------------------


def test_incident_references_the_canonical_diagnosis_object_not_a_copy():
    rounds = [_problem_round(i) for i in range(3)]
    incident = detect_incident(rounds, target="1.1.1.1")
    assert incident.diagnosis is rounds[-1].diagnosis


def test_incident_model_has_no_legacy_duplicate_fields():
    """Regression guard: TASK-009 removed likely_cause/confidence_pct/
    signals/explanation from Incident -- this task must not reintroduce
    anything resembling them."""
    field_names = {f.name for f in __import__("dataclasses").fields(Incident)}
    assert field_names == {"started_at", "ended_at", "severity", "target", "evidence", "diagnosis"}


# ---------------------------------------------------------------------------
# 5. Evidence preservation / no fabrication
# ---------------------------------------------------------------------------


def test_incident_evidence_is_exactly_the_diagnosis_evidence_no_fabrication():
    ev = _evidence(metric="gateway_latency", severity=Severity.CRITICAL)
    rounds = [_problem_round(i, evidence=[ev]) for i in range(3)]
    incident = detect_incident(rounds, target="1.1.1.1")
    assert incident.evidence == [ev]
    assert all(isinstance(e, Evidence) for e in incident.evidence)


def test_incident_evidence_accumulates_across_updates_without_duplicating():
    ev1 = _evidence(metric="gateway_latency")
    ev2 = _evidence(metric="dns_latency")
    opened = detect_incident([_problem_round(i, evidence=[ev1]) for i in range(3)], target="1.1.1.1")
    updated = detect_incident([_problem_round(i, evidence=[ev1, ev2]) for i in range(3, 6)], existing_incident=opened, target="1.1.1.1")
    assert ev1 in updated.evidence
    assert ev2 in updated.evidence
    assert updated.evidence.count(ev1) == 1  # not duplicated across the update


# ---------------------------------------------------------------------------
# 6/7/8. Severity behavior -- warning-level and critical evidence
# ---------------------------------------------------------------------------


def test_incident_severity_is_critical_when_supporting_evidence_is_critical():
    rounds = [_problem_round(i, evidence=[_evidence(severity=Severity.CRITICAL)]) for i in range(3)]
    incident = detect_incident(rounds, target="1.1.1.1")
    assert incident.severity == Severity.CRITICAL


def test_incident_severity_is_warning_when_supporting_evidence_is_only_warning():
    rounds = [_problem_round(i, evidence=[_evidence(severity=Severity.WARNING)]) for i in range(3)]
    incident = detect_incident(rounds, target="1.1.1.1")
    assert incident.severity == Severity.WARNING


def test_incident_severity_never_uses_a_static_latency_or_loss_threshold():
    """Regression guard: severity must come purely from Evidence.severity,
    never from re-deriving it out of a raw observed_value/expected_value
    threshold comparison."""
    import inspect

    from netscope.core import incidents as incidents_module

    source = inspect.getsource(incidents_module)
    for forbidden in ("LATENCY_GOOD_MS", "LATENCY_BAD_MS", "LOSS_BAD_PCT", "> 250", "> 5.0"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# 9. Untested evidence is not interpreted as confirmed healthy
# ---------------------------------------------------------------------------


def test_untested_evidence_inside_a_diagnosis_does_not_make_the_incident_look_resolved():
    """An INSUFFICIENT_EVIDENCE diagnosis (which is exactly what
    core.diagnosis.diagnose() returns for untested/missing evidence,
    per TASK-026) must behave like requirement 2 above, not like a
    healthy round -- this is the same untested-gateway principle,
    preserved one layer up."""
    opened = detect_incident([_problem_round(i) for i in range(3)], target="1.1.1.1")
    rounds = [_insufficient_round(i) for i in range(3, 6)]
    result = detect_incident(rounds, existing_incident=opened, target="1.1.1.1")
    assert result.is_active  # not silently resolved


# ---------------------------------------------------------------------------
# 10/11. Active / resolved incident semantics, duplicate avoidance
# ---------------------------------------------------------------------------


def test_continuing_matching_diagnoses_update_rather_than_duplicate():
    opened = detect_incident([_problem_round(i) for i in range(3)], target="1.1.1.1")
    continued = detect_incident([_problem_round(i) for i in range(3, 6)], existing_incident=opened, target="1.1.1.1")
    assert continued is not opened  # a fresh, updated object...
    assert continued.started_at == opened.started_at  # ...representing the SAME incident, not a duplicate


def test_three_consecutive_healthy_rounds_closes_an_active_incident():
    opened = detect_incident([_problem_round(i) for i in range(3)], target="1.1.1.1")
    closed = detect_incident([_healthy_round(i) for i in range(3, 6)], existing_incident=opened, target="1.1.1.1")
    assert closed.is_active is False
    assert closed.ended_at == _ts(3)


def test_a_resolved_incident_does_not_permanently_block_a_new_one():
    opened = detect_incident([_problem_round(i) for i in range(3)], target="1.1.1.1")
    closed = detect_incident([_healthy_round(i) for i in range(3, 6)], existing_incident=opened, target="1.1.1.1")
    assert closed.is_active is False

    reopened = detect_incident([_problem_round(i) for i in range(6, 9)], existing_incident=closed, target="1.1.1.1")
    assert reopened is not None
    assert reopened.is_active
    assert reopened.started_at == _ts(6)  # a genuinely new incident, not a resumption of the closed one


def test_single_healthy_round_inside_an_active_incident_does_not_close_it():
    opened = detect_incident([_problem_round(i) for i in range(3)], target="1.1.1.1")
    still_active = detect_incident([_healthy_round(3)], existing_incident=opened, target="1.1.1.1")
    assert still_active is opened


# ---------------------------------------------------------------------------
# 12. Confidence preservation -- traceable to the canonical diagnosis
# ---------------------------------------------------------------------------


def test_incident_confidence_is_traceable_via_the_referenced_diagnosis_not_fabricated():
    """Incident has no confidence field of its own (TASK-009) -- it's
    only reachable via incident.diagnosis.confidence, never invented by
    this module."""
    rounds = [_problem_round(i, confidence=0.73) for i in range(3)]
    incident = detect_incident(rounds, target="1.1.1.1")
    assert incident.diagnosis.confidence == 0.73
    assert not hasattr(incident, "confidence")


# ---------------------------------------------------------------------------
# 13. Core dependency isolation
# ---------------------------------------------------------------------------


def test_incidents_module_only_imports_stdlib_and_core_models():
    import netscope.core.incidents as incidents_module

    tree = ast.parse(open(incidents_module.__file__).read())
    top_level = set()
    full_paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level.add(alias.name.split(".")[0])
                full_paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
            full_paths.add(node.module)

    assert top_level <= {"__future__", "dataclasses", "datetime", "netscope"}
    assert full_paths <= {"__future__", "dataclasses", "datetime", "netscope.core.models"}
    for forbidden in ("netscope.ui", "netscope.adapters", "netscope.persistence", "netscope.core.diagnosis", "netscope.core.scoring", "netscope.core.baseline"):
        assert forbidden not in full_paths
