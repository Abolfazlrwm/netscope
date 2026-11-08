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

from netscope.app.use_cases import (
    REPORT_MAX_FAILURES,
    REPORT_MAX_ROUNDS,
    REPORT_ROWS_PER_TARGET,
    EvidenceReport,
    TargetReportRow,
)
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


# ---------------------------------------------------------------------------
# Evidence report (v0.5) -- `netscope report`. The use case hands us an
# EvidenceReport (app.use_cases); this module owns both canonical
# renderings: the `--json` payload and the exported markdown document.
# Markdown rules mirror the domain's honesty doctrine: absent metrics
# render as "--", omitted rows are *disclosed* with their count, and a
# bandwidth verdict only ever appears in the verdict column it belongs
# to.
# ---------------------------------------------------------------------------


def serialize_report_row(row: TargetReportRow) -> dict[str, Any]:
    """One evidence-table row -> JSON-safe dict."""
    return {
        "target": row.target,
        "timestamp": row.timestamp.isoformat(),
        "probe_type": row.probe_type.value,
        "success": row.success,
        "latency_ms": row.latency_ms,
        "packet_loss_pct": row.packet_loss_pct,
        "error": row.error,
        "error_type": row.error_type.value if row.error_type is not None else None,
        "verdict": row.verdict,
    }


def serialize_evidence_report(report: EvidenceReport) -> dict[str, Any]:
    """The canonical `--json` payload of `netscope report`."""
    return {
        "tool": "netscope",
        "report": "evidence",
        "window": {"since": report.since.isoformat(), "until": report.until.isoformat()},
        "scope": report.scope,
        "totals": {
            "targets": len(report.target_reports),
            "rounds": len(report.rounds),
            "measurements": report.measurements,
            "failures": report.failures,
            "mean_score": report.mean_score,
        },
        "rounds": [
            {
                "timestamp": r.timestamp.isoformat(),
                "experience": serialize_experience(r.experience) if r.experience else None,
                "measurements": [serialize_measurement(m) for m in r.measurements],
            }
            for r in report.rounds
        ],
        "targets": [
            {
                "target": t.target,
                "probe_types": t.probe_types,
                "measurements": t.measurements,
                "failures": t.failures,
                "mean_latency_ms": t.mean_latency_ms,
                "verdict_counts": t.verdict_counts,
                "truncated_rows": t.truncated,
                "rows": [serialize_report_row(row) for row in t.rows],
            }
            for t in report.target_reports
        ],
        "failures": [serialize_report_row(row) for row in report.failure_rows],
    }


def _md_cell(value: Any, *, limit: int = 60) -> str:
    """One markdown table cell: pipes escaped, newlines flattened,
    long values truncated with an ellipsis (never silently -- the
    character is visible)."""
    text = "--" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _verdict_summary(counts: dict[str, int]) -> str:
    return " · ".join(f"{name} ×{n}" for name, n in sorted(counts.items()))


def evidence_report_markdown(report: EvidenceReport) -> str:
    """Render the evidence report as a self-contained markdown document
    -- the artifact `netscope report [--out FILE]` writes. Every section
    only claims what the log proves; omission is disclosed, never silent."""
    lines: list[str] = []
    lines.append("# NetScope evidence report")
    lines.append("")
    days = (report.until - report.since).total_seconds() / 86400
    lines.append(f"- Window: {report.since.isoformat()} → {report.until.isoformat()} ({days:.0f} days)")
    scope = report.scope if report.scope is not None else "all targets"
    lines.append(f"- Scope: {scope}")
    mean = f"{report.mean_score:.1f}" if report.mean_score is not None else "--"
    lines.append(
        f"- Totals: {len(report.target_reports)} target(s) · {len(report.rounds)} round(s) · "
        f"{report.measurements} measurement(s) · {report.failures} failed · mean score {mean}"
    )
    lines.append("")
    lines.append("Source: the local measurement log (`~/.netscope/netscope.db`). Nothing here is re-measured -- it is what the log proves.")
    lines.append("")

    if report.measurements == 0:
        lines.append("No measurements in this window yet -- run `netscope diagnose` (or `netscope monitor`) so the log has evidence to report.")
        lines.append("")

    if report.rounds:
        lines.append("## Rounds")
        lines.append("")
        shown_rounds = report.rounds[-REPORT_MAX_ROUNDS:]
        omitted_rounds = len(report.rounds) - len(shown_rounds)
        lines.append("| when | score | level | checks | detail |")
        lines.append("|---|---|---|---:|---|")
        for r in shown_rounds:
            score = f"{r.experience.score:.1f}" if r.experience else "--"
            level = r.experience.level.value if r.experience else "untested"
            ok = sum(1 for m in r.measurements if m.success)
            failed = [m for m in r.measurements if not m.success]
            detail = "; ".join(f"{m.probe_type.value}: {m.error or 'failed'}" for m in failed) or "all checks ok"
            lines.append(f"| {_md_cell(r.timestamp.isoformat())} | {score} | {level} | {ok}/{len(r.measurements)} | {_md_cell(detail)} |")
        lines.append("")
        if omitted_rounds:
            lines.append(f"_… {omitted_rounds} older round(s) omitted (newest {REPORT_MAX_ROUNDS} shown)_")
            lines.append("")

    if report.target_reports:
        lines.append("## Per-target evidence")
        lines.append("")
        for t in report.target_reports:
            lines.append(f"### {_md_cell(t.target, limit=120)}")
            lines.append("")
            stats = f"`{' · '.join(t.probe_types)}` · {t.measurements} measurement(s) · {t.failures} failed"
            if t.mean_latency_ms is not None:
                stats += f" · mean latency {t.mean_latency_ms:.1f} ms"
            lines.append(stats)
            if t.verdict_counts:
                lines.append(f"verdicts: {_verdict_summary(t.verdict_counts)}")
            lines.append("")
            has_verdicts = any(row.verdict is not None for row in t.rows)
            header = "| when | probe | ok | latency_ms | loss % |"
            divider = "|---|---|---|---:|---:|"
            if has_verdicts:
                header += " verdict |"
                divider += "---|"
            header += " error |"
            divider += "---|"
            lines.append(header)
            lines.append(divider)
            for row in t.rows:
                cells = [
                    _md_cell(row.timestamp.isoformat()),
                    row.probe_type.value,
                    "yes" if row.success else "no",
                    _md_cell(f"{row.latency_ms:.1f}" if row.latency_ms is not None else None),
                    _md_cell(f"{row.packet_loss_pct:.1f}" if row.packet_loss_pct is not None else None),
                ]
                if has_verdicts:
                    cells.append(_md_cell(row.verdict))
                cells.append(_md_cell(row.error if row.error else (row.error_type.value if row.error_type is not None else None)))
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
            if t.truncated:
                lines.append(
                    f"_… {t.truncated} older row(s) omitted (newest {REPORT_ROWS_PER_TARGET} shown for this target; aggregates above cover the full window)_"
                )
                lines.append("")

    if report.failure_rows:
        lines.append("## Failures")
        lines.append("")
        shown_failures = report.failure_rows[-REPORT_MAX_FAILURES:]
        omitted_failures = len(report.failure_rows) - len(shown_failures)
        for row in shown_failures:
            label = row.error or (row.error_type.value if row.error_type is not None else "failed")
            lines.append(f"- {row.timestamp.isoformat()} — {row.probe_type.value} `{row.target}` → {_md_cell(label, limit=90)}")
        lines.append("")
        if omitted_failures:
            lines.append(f"_… {omitted_failures} older failure(s) omitted (newest {REPORT_MAX_FAILURES} shown; totals above cover the full window)_")
            lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Round scores are recomputed against the *current* personal baseline; the log alone cannot reconstruct the baseline as it stood at each round's time (the same honest approximation `netscope history` makes)."
    )
    lines.append(
        "- `verdict` is the v0.4 baseline-relative throughput classification: within ±15% of your usual Mbps reads as `as_usual`; beyond that, `slower_than_usual` / `faster_than_usual`."
    )
    lines.append("- Untested metrics never read as healthy: absent evidence renders as `--`, never as a passing value.")
    lines.append(
        "- Throughput evidence stays throughput evidence: verdicts live in the verdict column, latency means only ever average successful latency readings."
    )
    lines.append("")
    return "\n".join(lines)
