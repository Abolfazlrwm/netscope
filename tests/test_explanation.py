"""
Characterization tests for netscope.explanation.explainer.

Per the audit, this layer is deliberately "just formatting" -- it takes
an already-computed Diagnosis and produces human-readable text. We test
that it doesn't crash, includes the key facts, and doesn't silently drop
information -- not the exact wording, which is not part of any public
contract.
"""

from __future__ import annotations

from netscope.diagnosis.engine import Diagnosis
from netscope.explanation.explainer import explain


def test_explain_includes_the_likely_cause_and_confidence():
    d = Diagnosis(
        likely_cause="Local network issue (Wi-Fi congestion, router overload, or bad cabling)",
        confidence_pct=80.0,
        evidence=["Latency/packet loss to the local gateway is elevated."],
        ruled_out=["ISP-side or destination-side cause (local hop already fails)"],
    )
    text = explain(d)

    assert "Local network issue" in text
    assert "80%" in text


def test_explain_includes_every_evidence_line():
    d = Diagnosis(
        likely_cause="Upstream ISP issue",
        confidence_pct=70.0,
        evidence=["evidence line one", "evidence line two"],
        ruled_out=[],
    )
    text = explain(d)

    assert "evidence line one" in text
    assert "evidence line two" in text


def test_explain_includes_every_ruled_out_line():
    d = Diagnosis(
        likely_cause="Destination-specific issue",
        confidence_pct=65.0,
        evidence=["some evidence"],
        ruled_out=["ruled out one", "ruled out two"],
    )
    text = explain(d)

    assert "ruled out one" in text
    assert "ruled out two" in text


def test_explain_does_not_crash_with_empty_evidence_and_ruled_out():
    d = Diagnosis(
        likely_cause="No issue detected",
        confidence_pct=90.0,
        evidence=[],
        ruled_out=[],
    )
    text = explain(d)
    assert "No issue detected" in text
    assert "90%" in text


def test_explain_returns_a_single_string():
    d = Diagnosis(likely_cause="x", confidence_pct=50.0, evidence=[], ruled_out=[])
    assert isinstance(explain(d), str)
