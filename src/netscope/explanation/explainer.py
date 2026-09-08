"""
Human explanation layer.

Differentiator #7: kept deliberately separate from core.diagnosis so the
wording/tone/language can change without touching diagnostic logic.

TASK-026 migration: updated to consume the canonical
netscope.core.models.Diagnosis (classification: Hypothesis, evidence:
list[Evidence], confidence: 0.0-1.0, ruled_out: list[Evidence | Hypothesis])
in place of the retired diagnosis.engine.Diagnosis (likely_cause: str,
confidence_pct: float, evidence: list[str], ruled_out: list[str]). This
is a required consumer migration, not an unrelated change:
diagnosis.engine.Diagnosis no longer exists once core/diagnosis.py
replaces diagnosis/engine.py, so this module's import would otherwise be
broken. The formatting logic changed only as much as the new structured
types require -- turning an Evidence/Hypothesis object into a line of
text via `_describe`, instead of a value that was already a ready-made
string.
"""

from __future__ import annotations

from netscope.core.models import Diagnosis, Evidence, Hypothesis

_HYPOTHESIS_LABELS: dict[Hypothesis, str] = {
    Hypothesis.LOCAL_NETWORK_ISSUE: "Local network issue",
    Hypothesis.ISP_ACCESS_ISSUE: "Upstream ISP issue",
    Hypothesis.DNS_ISSUE: "DNS issue",
    Hypothesis.ROUTING_DEGRADATION: "Routing degradation",
    Hypothesis.DESTINATION_ISSUE: "Destination-specific issue",
    Hypothesis.SERVICE_ISSUE: "Service issue",
    Hypothesis.GENERAL_CONNECTIVITY_ISSUE: "General connectivity issue",
    Hypothesis.INSUFFICIENT_EVIDENCE: "Not enough evidence to diagnose confidently",
}


def _describe(item: Evidence | Hypothesis) -> str:
    """One human-readable line for either kind of ruled_out/evidence
    item -- Diagnosis.ruled_out is deliberately list[Evidence | Hypothesis]
    (core/models.py, TASK-009), so this layer has to handle both."""
    if isinstance(item, Hypothesis):
        return _HYPOTHESIS_LABELS[item]
    if not item.tested:
        return f"{item.metric}: not tested"
    if item.observed_value is not None and item.expected_value is not None:
        return f"{item.metric}: observed {item.observed_value} (expected ~{item.expected_value})"
    if item.observed_value is not None:
        return f"{item.metric}: observed {item.observed_value}"
    return f"{item.metric}: tested, no value recorded"


def explain(diagnosis: Diagnosis) -> str:
    label = _HYPOTHESIS_LABELS[diagnosis.classification]
    lines = [f"Likely cause: {label} (confidence: {diagnosis.confidence * 100:.0f}%)"]
    if diagnosis.evidence:
        lines.append("Why we think this:")
        lines.extend(f"  - {_describe(e)}" for e in diagnosis.evidence)
    if diagnosis.ruled_out:
        lines.append("Ruled out:")
        lines.extend(f"  - {_describe(r)}" for r in diagnosis.ruled_out)
    return "\n".join(lines)
