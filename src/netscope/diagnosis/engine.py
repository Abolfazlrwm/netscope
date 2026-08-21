"""
Diagnosis engine (MVP rule-based version).

Differentiator #4 from the research doc: instead of just showing a
number, we produce an evidence chain: symptom -> correlated signals ->
likely cause (with a confidence %) -> what we ruled out.

This MVP version uses hand-written rules that compare measurements
against multiple targets (e.g. router vs. public DNS vs. public CDN)
to localize whether a problem is local, ISP-side, or destination-specific
(Differentiator #6, "Service Intelligence"). It is intentionally simple
and meant to be replaced/extended, not to be the final word.
"""

from __future__ import annotations

from dataclasses import dataclass

from netscope.core.models import RawMeasurement


@dataclass
class Diagnosis:
    likely_cause: str
    confidence_pct: float
    evidence: list[str]
    ruled_out: list[str]


def diagnose(
    local_gateway: RawMeasurement | None,
    public_dns: RawMeasurement | None,
    public_cdn: RawMeasurement | None,
) -> Diagnosis:
    """Very small decision tree comparing three reference points:

    - local_gateway: ping to the user's own router/gateway
    - public_dns: ping to a well-known public resolver (e.g. 1.1.1.1)
    - public_cdn: ping/http to a well-known public CDN endpoint

    The logic: if the local hop is already bad, the problem is almost
    certainly local (Wi-Fi/router). If the local hop is fine but
    everything beyond it is bad, the problem is upstream (ISP or
    further). If only the CDN target is bad, it's destination-specific.
    """

    evidence: list[str] = []
    ruled_out: list[str] = []

    def is_bad(m: RawMeasurement | None) -> bool:
        if m is None:
            return False
        if not m.success:
            return True
        if m.latency_ms is not None and m.latency_ms > 250:
            return True
        if m.packet_loss_pct is not None and m.packet_loss_pct > 5:
            return True
        return False

    gw_bad = is_bad(local_gateway)
    dns_bad = is_bad(public_dns)
    cdn_bad = is_bad(public_cdn)

    if gw_bad:
        evidence.append("Latency/packet loss to the local gateway is elevated.")
        ruled_out.append("ISP-side or destination-side cause (local hop already fails)")
        return Diagnosis(
            likely_cause="Local network issue (Wi-Fi congestion, router overload, or bad cabling)",
            confidence_pct=80.0,
            evidence=evidence,
            ruled_out=ruled_out,
        )

    if not gw_bad and dns_bad and cdn_bad:
        evidence.append("Local gateway is healthy, but both a public DNS resolver and a public CDN show elevated latency/loss.")
        ruled_out.append("Local network issue (gateway is healthy)")
        ruled_out.append("Destination-specific issue (multiple independent destinations affected)")
        return Diagnosis(
            likely_cause="Upstream ISP issue (problem beyond your home network)",
            confidence_pct=70.0,
            evidence=evidence,
            ruled_out=ruled_out,
        )

    if not gw_bad and not dns_bad and cdn_bad:
        evidence.append("Local gateway and public DNS resolver are healthy, but the CDN/service target is degraded.")
        ruled_out.append("Local network issue")
        ruled_out.append("General ISP/upstream issue (other destinations are fine)")
        return Diagnosis(
            likely_cause="Destination-specific issue (the remote service/CDN itself, not your connection)",
            confidence_pct=65.0,
            evidence=evidence,
            ruled_out=ruled_out,
        )

    evidence.append("All reference points (local gateway, public DNS, public CDN) look healthy.")
    return Diagnosis(
        likely_cause="No issue detected",
        confidence_pct=90.0,
        evidence=evidence,
        ruled_out=[],
    )
