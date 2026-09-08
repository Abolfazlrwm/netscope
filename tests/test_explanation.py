"""
Characterization tests for netscope.explanation.explainer.

Per the audit, this layer is deliberately "just formatting" -- it takes
an already-computed Diagnosis and produces human-readable text. We test
that it doesn't crash, includes the key facts, and doesn't silently drop
information -- not the exact wording, which is not part of any public
contract.

TASK-026 migration: rewritten against the canonical
netscope.core.models.Diagnosis (classification: Hypothesis, evidence:
list[Evidence], confidence: 0.0-1.0, ruled_out: list[Evidence | Hypothesis])
in place of the retired diagnosis.engine.Diagnosis
(likely_cause: str, confidence_pct: float, evidence: list[str],
ruled_out: list[str]). Each test below preserves the exact same testing
intent as its predecessor (see git history), just expressed with the new
structured types instead of free-form strings.
"""

from __future__ import annotations

from netscope.core.models import Diagnosis, Evidence, Hypothesis, Severity
from netscope.explanation.explainer import explain


def test_explain_includes_the_likely_cause_and_confidence():
    d = Diagnosis(
        classification=Hypothesis.LOCAL_NETWORK_ISSUE,
        confidence=0.80,
        evidence=[
            Evidence(
                metric="gateway_latency",
                observed_value=300.0,
                expected_value=20.0,
                deviation=6.0,
                severity=Severity.CRITICAL,
            )
        ],
        ruled_out=[Hypothesis.ISP_ACCESS_ISSUE],
    )
    text = explain(d)

    assert "Local network issue" in text
    assert "80%" in text


def test_explain_includes_every_evidence_line():
    d = Diagnosis(
        classification=Hypothesis.ISP_ACCESS_ISSUE,
        confidence=0.70,
        evidence=[
            Evidence(metric="dns_latency", observed_value=200.0, severity=Severity.CRITICAL),
            Evidence(metric="destination_latency", observed_value=250.0, severity=Severity.CRITICAL),
        ],
        ruled_out=[],
    )
    text = explain(d)

    assert "dns_latency" in text
    assert "destination_latency" in text


def test_explain_includes_every_ruled_out_line():
    """ruled_out mixes Evidence and Hypothesis items (core/models.py,
    TASK-009) -- both must render, not just one type."""
    d = Diagnosis(
        classification=Hypothesis.DESTINATION_ISSUE,
        confidence=0.65,
        evidence=[Evidence(metric="destination_latency", observed_value=250.0, severity=Severity.CRITICAL)],
        ruled_out=[
            Evidence(metric="gateway_latency", observed_value=10.0, severity=Severity.INFO),
            Hypothesis.LOCAL_NETWORK_ISSUE,
        ],
    )
    text = explain(d)

    assert "gateway_latency" in text
    assert "Local network issue" in text


def test_explain_includes_an_untested_evidence_line_without_a_fabricated_value():
    """This is the explanation-layer side of the untested-gateway fix:
    an untested metric must render as "not tested", never as if a value
    had been observed."""
    d = Diagnosis(
        classification=Hypothesis.INSUFFICIENT_EVIDENCE,
        confidence=0.0,
        evidence=[Evidence(metric="gateway_latency", tested=False)],
        ruled_out=[],
    )
    text = explain(d)

    assert "gateway_latency: not tested" in text


def test_explain_does_not_crash_with_empty_evidence_and_ruled_out():
    d = Diagnosis(classification=Hypothesis.INSUFFICIENT_EVIDENCE, confidence=0.0, evidence=[], ruled_out=[])
    text = explain(d)
    assert "Not enough evidence to diagnose confidently" in text
    assert "0%" in text


def test_explain_returns_a_single_string():
    d = Diagnosis(classification=Hypothesis.GENERAL_CONNECTIVITY_ISSUE, confidence=0.5, evidence=[], ruled_out=[])
    assert isinstance(explain(d), str)
