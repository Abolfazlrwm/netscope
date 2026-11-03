"""
netscope.app.reports

Serializable, machine-readable renderings of NetScope's domain results
-- the single place a Diagnosis, ExperienceEvent, or RawMeasurement is
turned into plain JSON-compatible data.

WHY THIS LIVES IN app
---------------------
Serialization is presentation-adjacent application logic: it reads
domain types and produces transport-neutral dicts, touching neither
adapters nor persistence. `ui/cli.py` (the `--json` flag) and
`ui/tui` (history views) both consume these builders instead of each
inventing its own dict-shape for the same domain objects -- one
canonical wire format, like `explanation/explainer.py` is the one
canonical human format.

JSON-SAFETY CONTRACT
--------------------
Every returned object contains only str/int/float/bool/None/list/dict
-- safe for `json.dumps` as-is. Enum values are their `.value` strings;
datetimes are ISO-8601 with UTC offset (the same form persistence
already writes). `RawMeasurement.extra` is passed through only when
JSON-safe: `asdict`-style recursion would choke on a RouteSnapshot
payload (traceroute measurements keep a real RouteSnapshot object in
extra["route"]), so non-primitive extra payloads are replaced with a
type note rather than half-serializing.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import (
    Diagnosis,
    Evidence,
    ExperienceEvent,
    Hypothesis,
    RawMeasurement,
    RouteHop,
    RouteSnapshot,
)

_JSON_SCALARS = (str, int, float, bool, type(None))


def _json_safe(value: Any) -> Any:
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if not isinstance(v, (bytes, bytearray))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    # Non-primitive domain payload (e.g. traceroute's RouteSnapshot):
    # report its presence honestly instead of half-serializing it.
    return f"<{type(value).__name__}>"


def serialize_measurement(measurement: RawMeasurement) -> dict[str, Any]:
    return {
        "probe_type": measurement.probe_type.value,
        "target": measurement.target,
        "timestamp": measurement.timestamp.isoformat(),
        "success": measurement.success,
        "latency_ms": measurement.latency_ms,
        "packet_loss_pct": measurement.packet_loss_pct,
        "jitter_ms": measurement.jitter_ms,
        "error": measurement.error,
        "error_type": measurement.error_type.value if measurement.error_type is not None else None,
        "extra": _json_safe(measurement.extra),
    }


def serialize_evidence(evidence: Evidence) -> dict[str, Any]:
    return {
        "metric": evidence.metric,
        "tested": evidence.tested,
        "observed_value": evidence.observed_value,
        "expected_value": evidence.expected_value,
        "deviation": evidence.deviation,
        "severity": evidence.severity.value,
        "confidence": evidence.confidence,
    }


def serialize_diagnosis(diagnosis: Diagnosis | None) -> dict[str, Any] | None:
    """Diagnosis -> dict. None (the confirmed-healthy convention) is
    passed through as None -- the JSON consumer sees the same three
    states the domain has: healthy (null), insufficient evidence, or a
    confirmed classification."""
    if diagnosis is None:
        return None
    return {
        "classification": diagnosis.classification.value,
        "confidence": diagnosis.confidence,
        "timestamp": diagnosis.timestamp.isoformat(),
        "evidence": [serialize_evidence(e) for e in diagnosis.evidence],
        "ruled_out": [serialize_evidence(item) if isinstance(item, Evidence) else {"hypothesis": item.value} for item in diagnosis.ruled_out],
    }


def serialize_experience(experience: ExperienceEvent) -> dict[str, Any]:
    return {
        "score": experience.score,
        "level": experience.level.value,
        "timestamp": experience.timestamp.isoformat(),
    }


def build_diagnose_report(
    measurements: list[RawMeasurement],
    experience: ExperienceEvent,
    diagnosis: Diagnosis | None,
    *,
    gateway: str | None = None,
) -> dict[str, Any]:
    """The canonical `--json` payload of one `netscope diagnose` run."""
    return {
        "tool": "netscope",
        "report": "diagnose",
        "gateway": gateway,
        "experience": serialize_experience(experience),
        "diagnosis": serialize_diagnosis(diagnosis),
        "measurements": [serialize_measurement(m) for m in measurements],
    }


HYPOTHESIS_SUMMARY_LABELS: dict[Hypothesis, str] = {
    Hypothesis.LOCAL_NETWORK_ISSUE: "Local network issue",
    Hypothesis.ISP_ACCESS_ISSUE: "Upstream ISP issue",
    Hypothesis.DNS_ISSUE: "DNS issue",
    Hypothesis.ROUTING_DEGRADATION: "Routing degradation",
    Hypothesis.DESTINATION_ISSUE: "Destination-specific issue",
    Hypothesis.SERVICE_ISSUE: "Service issue",
    Hypothesis.GENERAL_CONNECTIVITY_ISSUE: "General connectivity issue",
    # Same wording the human explainer uses -- one canonical label per
    # hypothesis across the JSON and human formats.
    Hypothesis.INSUFFICIENT_EVIDENCE: "Not enough evidence to diagnose confidently",
}


def serialize_route_hops(hops: list[RouteHop]) -> list[dict[str, Any]]:
    """RouteHop list -> JSON-safe dicts (the `--json` shape of
    `netscope route`). Address/host/asn/country are all optional in the
    domain (unresolved hops are honest None, never '*'); the '*' glyph
    is a presentation concern owned by ui/render.py."""
    return [
        {
            "ttl": hop.ttl,
            "address": hop.address,
            "hostname": hop.hostname,
            "asn": hop.asn,
            "organization": hop.organization,
            "country": hop.country,
            "avg_rtt_ms": hop.avg_rtt_ms,
            "packet_loss_pct": hop.packet_loss_pct,
        }
        for hop in hops
    ]


def serialize_route_snapshot(snapshot: RouteSnapshot) -> dict[str, Any]:
    return {
        "target": snapshot.target,
        "timestamp": snapshot.timestamp.isoformat(),
        "hops": serialize_route_hops(snapshot.hops),
    }
