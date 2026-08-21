"""
Human explanation layer.

Differentiator #7: kept deliberately separate from diagnosis.engine so the
wording/tone/language can change without touching diagnostic logic.
"""

from __future__ import annotations

from netscope.diagnosis.engine import Diagnosis


def explain(diagnosis: Diagnosis) -> str:
    lines = [
        f"Likely cause: {diagnosis.likely_cause} (confidence: {diagnosis.confidence_pct:.0f}%)",
    ]
    if diagnosis.evidence:
        lines.append("Why we think this:")
        lines.extend(f"  - {e}" for e in diagnosis.evidence)
    if diagnosis.ruled_out:
        lines.append("Ruled out:")
        lines.extend(f"  - {r}" for r in diagnosis.ruled_out)
    return "\n".join(lines)
