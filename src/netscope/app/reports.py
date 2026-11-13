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

import html
import math
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
from netscope.core.routing import RouteChange

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


def serialize_route_change(change: RouteChange) -> dict[str, Any]:
    """One RouteChange -> JSON-safe dict (the `--json` shape of
    `netscope route`'s `stability.last_change`, v0.8). The hop diff is
    included in full here -- the human rendering caps at three hops,
    but machine consumers get every moved position."""
    return {
        "previous_timestamp": change.previous_timestamp.isoformat(),
        "new_timestamp": change.new_timestamp.isoformat(),
        "changed_hops": [
            {
                "ttl": hop_change.ttl,
                "previous_address": hop_change.previous_address,
                "new_address": hop_change.new_address,
            }
            for hop_change in change.changed_hops
        ],
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
        "jitter_ms": row.jitter_ms,
        "error": row.error,
        "error_type": row.error_type.value if row.error_type is not None else None,
        "verdict": row.verdict,
        "geo": row.geo,
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
                "geo": t.geo,
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


def geo_place_label(geo: dict) -> str | None:
    """One shared wording for a geo block's place, used by the rich
    detail, the markdown geography section and the HTML one -- the
    one-shared-wording pattern the verdict summary established.
    Reads only canonical `core.geo.geo_extra` keys; a block missing
    both names (coordinates-only) renders as None -- the distance
    speaks for it."""
    parts = [geo.get(part) for part in ("city", "country")]
    label = ", ".join(str(part) for part in parts if part)
    return label or None


def geo_summary(geo: dict) -> str:
    """One shared one-line geography summary: place, distance from
    home when recorded, and the database's age with the aging notice
    the EULA policy asks for. Every renderer shows exactly this."""
    parts: list[str] = []
    place = geo_place_label(geo)
    if place:
        parts.append(place)
    distance = geo.get("distance_km")
    if isinstance(distance, (int, float)) and distance >= 1:
        parts.append(f"{distance:,.0f} km from home")
    age = geo.get("db_age_days")
    if isinstance(age, int):
        stale = geo.get("stale_db") is True
        suffix = " -- refresh recommended" if stale else ""
        parts.append(f"db {age}d old{suffix}")
    return " · ".join(parts)


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
            header = "| when | probe | ok | latency_ms | loss % | jitter_ms |"
            divider = "|---|---|---|---:|---:|---:|"
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
                    _md_cell(f"{row.jitter_ms:.1f}" if row.jitter_ms is not None else None),
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

    # Geography (v0.12): the persisted place/distance facts, per
    # target, only when any target actually has one -- honest absence
    # means no section at all, never an empty table.
    geo_targets = [t for t in report.target_reports if t.geo is not None]
    if geo_targets:
        lines.append("## Geography")
        lines.append("")
        for t in geo_targets:
            assert t.geo is not None
            lines.append(f"- `{t.target}` → {geo_summary(t.geo)}")
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
    lines.append(
        "- Geography rows are the place/distance the NEWEST geo-aware measurement of that target recorded in the window -- looked up at run time from the user's own GeoLite2 database, never re-looked-up here; a stale-database notice rides along when the database has aged past its refresh rhythm."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering (v0.10) -- the same EvidenceReport, dressed for humans
# who don't read terminals: one self-contained page, dark theme, inline
# SVG sparklines and a score gauge, zero external assets, zero scripts.
# The honesty rules are the markdown's own, verbatim -- same caps, same
# disclosures, same "--" for absent metrics -- so the two renderings can
# never disagree about the facts; they only disagree about the font.
# ---------------------------------------------------------------------------


def _esc(value: Any) -> str:
    """HTML-escape any interpolated text. Targets and errors are
    user-controlled strings (config hosts, probe library messages) --
    they get out of here as text, never as markup."""
    return html.escape(str(value), quote=True)


def _score_color(score: float) -> str:
    """The score band colors, mirroring the CLI's level styles: green
    for excellent/good, amber for degraded, red for poor/down."""
    if score >= 70:
        return "#3fb950"
    if score >= 40:
        return "#d29922"
    return "#f85149"


def _svg_sparkline(values: Any, *, width: int = 640, height: int = 72, stroke: str = "#58a6ff") -> str:
    """A polyline sparkline as inline SVG -- the HTML twin of the
    terminal's Unicode sparkline. None entries are skipped (absence of
    evidence is not a dip to zero); fewer than two values is not a
    trend, and renders as nothing -- the caller shows an honest
    "not enough data" note instead of a fake flat line."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return ""
    low, high = min(clean), max(clean)
    pad = 6.0
    span = (high - low) or 1.0
    step = (width - 2 * pad) / (len(clean) - 1)
    points = " ".join(f"{pad + i * step:.1f},{height - pad - (v - low) / span * (height - 2 * pad):.1f}" for i, v in enumerate(clean))
    last_x, last_y = points.split()[-1].split(",")
    dot_color = stroke
    return (
        f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="trend sparkline">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="2" stroke-linejoin="round" '
        f'stroke-linecap="round" points="{points}"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="3" fill="{dot_color}"/></svg>'
    )


def _svg_gauge(score: float, *, size: int = 96) -> str:
    """A donut gauge for one score: the arc length IS the score, the
    color IS the level band. No animation, no script -- a static,
    printable claim."""
    radius = size / 2 - 8
    circumference = 2 * math.pi * radius
    filled = max(0.0, min(100.0, score)) / 100 * circumference
    color = _score_color(score)
    center = size / 2
    return (
        f'<svg class="gauge" width="{size}" height="{size}" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="mean score {score:.0f} of 100">'
        f'<circle cx="{center}" cy="{center}" r="{radius}" fill="none" stroke="#30363d" stroke-width="8"/>'
        f'<circle cx="{center}" cy="{center}" r="{radius}" fill="none" stroke="{color}" stroke-width="8" '
        f'stroke-linecap="round" stroke-dasharray="{filled:.1f} {circumference:.1f}" '
        f'transform="rotate(-90 {center} {center})"/>'
        f'<text x="{center}" y="{center + 6}" text-anchor="middle" class="gauge-num">{score:.0f}</text></svg>'
    )


_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0 auto; padding: 32px 20px 48px; max-width: 880px;
  background: #0d1117; color: #c9d1d9; line-height: 1.55;
  font: 15px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
code, .num, td, .stat b { font-family: ui-monospace, "Cascadia Code", Consolas, Menlo, monospace; }
h1 { font-size: 22px; margin: 0 0 2px; color: #e6edf3; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .08em;
  color: #8b949e; margin: 36px 0 10px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
h3 { font-size: 16px; margin: 22px 0 6px; color: #e6edf3; }
a { color: #58a6ff; }
.window { color: #8b949e; margin: 0; font-size: 13px; }
header { border-bottom: 1px solid #21262d; padding-bottom: 16px; }
.totals { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0 0; }
.stat { background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  padding: 10px 14px; min-width: 108px; }
.stat .label { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; }
.stat b { display: block; font-size: 20px; color: #e6edf3; margin-top: 2px; }
.sparkline-box { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 14px; }
.sparkline-box svg { display: block; max-width: 100%; height: auto; }
.no-data { color: #8b949e; font-style: italic; }
.gauge-wrap { display: flex; align-items: center; gap: 18px; }
.gauge text.gauge-num { fill: #e6edf3; font-size: 20px;
  font-family: ui-monospace, Consolas, Menlo, monospace; }
.agg { color: #8b949e; font-size: 13px; margin: 0 0 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 6px 0 10px; }
th { text-align: left; color: #8b949e; font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: .06em; }
th, td { border: 1px solid #21262d; padding: 5px 9px; }
tbody tr:nth-child(odd) { background: #10151c; }
td.num, th.num { text-align: right; }
.ok { color: #3fb950; } .bad { color: #f85149; } .warn { color: #d29922; }
.dim { color: #8b949e; }
.disclosure { color: #8b949e; font-style: italic; font-size: 12.5px; margin: 2px 0 0; }
ul.notes { padding-left: 20px; } ul.notes li { margin: 6px 0; }
footer { margin-top: 40px; border-top: 1px solid #21262d; padding-top: 14px;
  color: #8b949e; font-size: 12.5px; }
.fail-tag { color: #f85149; }
"""


def evidence_report_html(report: EvidenceReport) -> str:
    """Render the evidence report as one self-contained HTML page --
    the artifact `netscope report --html FILE` writes (v0.10).

    Same facts as `evidence_report_markdown`, same caps, same
    disclosures: the newest-rounds and per-target row caps come from the
    same constants, omitted counts are stated in place, absent metrics
    render as "--", and round scores carry the recomputed-against-the-
    current-baseline caveat. Presentation only: no scripts, no external
    assets, no re-measurement -- the file works offline, forever."""
    days = (report.until - report.since).total_seconds() / 86400
    scope = _esc(report.scope) if report.scope is not None else "all targets"
    mean_html = f"{report.mean_score:.1f}" if report.mean_score is not None else "--"

    out: list[str] = []
    out.append("<!doctype html>")
    out.append('<html lang="en">')
    out.append("<head>")
    out.append('<meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append(f"<title>NetScope evidence report — {scope} ({days:.0f} days)</title>")
    out.append(f"<style>{_CSS}</style>")
    out.append("</head>")
    out.append("<body>")

    out.append("<header>")
    out.append("<h1>NetScope evidence report</h1>")
    out.append(
        f'<p class="window">window: {_esc(report.since.isoformat())} → {_esc(report.until.isoformat())} ({days:.0f} days) · scope: {scope}</p>'
    )
    out.append("</header>")

    # Totals strip -- the same numbers the markdown header line claims.
    out.append('<section class="totals">')
    for label, value in (
        ("targets", str(len(report.target_reports))),
        ("rounds", str(len(report.rounds))),
        ("measurements", str(report.measurements)),
        ("failures", str(report.failures)),
        ("mean score", mean_html),
    ):
        out.append(f'<div class="stat"><span class="label">{label}</span><b>{_esc(value)}</b></div>')
    out.append("</section>")

    if report.measurements == 0:
        out.append(
            "<h2>Nothing in this window</h2>"
            "<p class='no-data'>No measurements in this window yet — run "
            "<code>netscope diagnose</code> (or <code>netscope monitor</code>) so the log "
            "has evidence to report.</p>"
        )

    # Score trend: the gauge + the rounds sparkline, one visual unit.
    if report.rounds:
        scores = [r.experience.score for r in report.rounds if r.experience]
        shown_rounds = report.rounds[-REPORT_MAX_ROUNDS:]
        omitted_rounds = len(report.rounds) - len(shown_rounds)
        out.append("<h2>Score trend</h2>")
        out.append('<div class="sparkline-box">')
        if report.mean_score is not None:
            out.append('<div class="gauge-wrap">')
            out.append(_svg_gauge(report.mean_score))
            out.append(
                '<div><div class="dim">mean score across the window</div>'
                '<div class="dim">recomputed against the <em>current</em> personal baseline — '
                "the log alone cannot reconstruct the baseline as it stood at each round's time</div></div>"
            )
            out.append("</div>")
        spark = _svg_sparkline(scores)
        if spark:
            out.append(spark)
            low, high = min(scores), max(scores)
            out.append(f'<p class="disclosure">score trend, oldest → newest · low {low:.0f} · high {high:.0f} over {len(scores)} scored round(s)</p>')
        else:
            out.append("<p class='no-data'>Not enough scored rounds for a trend — the line appears once two rounds carry a score.</p>")
        if omitted_rounds:
            out.append(f"<p class='disclosure'>… {omitted_rounds} older round(s) omitted (newest {REPORT_MAX_ROUNDS} shown)</p>")
        out.append("</div>")

        out.append("<h2>Rounds</h2>")
        out.append("<table><thead><tr><th>when</th><th class='num'>score</th><th>level</th><th>checks</th><th>detail</th></tr></thead><tbody>")
        for r in shown_rounds:
            score = f"{r.experience.score:.1f}" if r.experience else "--"
            level = r.experience.level.value if r.experience else "untested"
            ok = sum(1 for m in r.measurements if m.success)
            failed = [m for m in r.measurements if not m.success]
            detail = "; ".join(f"{m.probe_type.value}: {m.error or 'failed'}" for m in failed) or "all checks ok"
            out.append(
                "<tr>"
                f"<td>{_esc(r.timestamp.isoformat())}</td>"
                f"<td class='num'>{_esc(score)}</td>"
                f"<td>{_esc(level)}</td>"
                f"<td class='num'>{ok}/{len(r.measurements)}</td>"
                f"<td class='dim'>{_esc(detail)}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")

    # Per-target evidence: aggregates, latency sparkline, evidence table.
    if report.target_reports:
        out.append("<h2>Per-target evidence</h2>")
        for t in report.target_reports:
            out.append(f"<h3><code>{_esc(t.target)}</code></h3>")
            stats = f"{' · '.join(t.probe_types)} · {t.measurements} measurement(s) · {t.failures} failed"
            if t.mean_latency_ms is not None:
                stats += f" · mean latency {t.mean_latency_ms:.1f} ms"
            out.append(f"<p class='agg'>{_esc(stats)}")
            if t.verdict_counts:
                out.append(f"<br>verdicts: {_esc(_verdict_summary(t.verdict_counts))}")
            out.append("</p>")
            latency_spark = _svg_sparkline([row.latency_ms for row in t.rows], stroke="#3fb950", height=56, width=640)
            if latency_spark:
                out.append('<div class="sparkline-box">')
                out.append(latency_spark)
                out.append("<p class='disclosure'>latency per shown measurement, oldest → newest (ms)</p>")
                out.append("</div>")
            has_verdicts = any(row.verdict is not None for row in t.rows)
            out.append(
                "<table><thead><tr><th>when</th><th>probe</th><th>ok</th><th class='num'>latency_ms</th><th class='num'>loss %</th><th class='num'>jitter_ms</th>"
            )
            if has_verdicts:
                out.append("<th>verdict</th>")
            out.append("<th>error</th></tr></thead><tbody>")
            for row in t.rows:
                cells = [
                    f"<td>{_esc(row.timestamp.isoformat())}</td>",
                    f"<td>{_esc(row.probe_type.value)}</td>",
                    "<td class='ok'>yes</td>" if row.success else "<td class='bad'>no</td>",
                    f"<td class='num'>{f'{row.latency_ms:.1f}' if row.latency_ms is not None else '--'}</td>",
                    f"<td class='num'>{f'{row.packet_loss_pct:.1f}' if row.packet_loss_pct is not None else '--'}</td>",
                    f"<td class='num'>{f'{row.jitter_ms:.1f}' if row.jitter_ms is not None else '--'}</td>",
                ]
                if has_verdicts:
                    cells.append(f"<td class='dim'>{_esc(row.verdict) if row.verdict else '--'}</td>")
                error_label = row.error or (row.error_type.value if row.error_type is not None else None)
                cells.append(f"<td class='dim'>{_esc(error_label) if error_label else '--'}</td>")
                out.append("<tr>" + "".join(cells) + "</tr>")
            out.append("</tbody></table>")
            if t.truncated:
                out.append(
                    f"<p class='disclosure'>… {t.truncated} older row(s) omitted (newest {REPORT_ROWS_PER_TARGET} "
                    "shown for this target; aggregates above cover the full window)</p>"
                )

    # Geography (v0.12) -- the markdown's section, word for word.
    geo_targets = [t for t in report.target_reports if t.geo is not None]
    if geo_targets:
        out.append("<h2>Geography</h2>")
        out.append("<table><thead><tr><th>target</th><th>where it is</th><th>as measured</th></tr></thead><tbody>")
        for t in geo_targets:
            assert t.geo is not None
            place = geo_place_label(t.geo) or "--"
            stale = t.geo.get("stale_db") is True
            summary_class = "warn" if stale else "dim"
            out.append(
                f"<tr><td><code>{_esc(t.target)}</code></td><td>{_esc(place)}</td><td class='{summary_class}'>{_esc(geo_summary(t.geo))}</td></tr>"
            )
        out.append("</tbody></table>")

    # Failures, verbatim, capped and disclosed like the markdown.
    if report.failure_rows:
        shown_failures = report.failure_rows[-REPORT_MAX_FAILURES:]
        omitted_failures = len(report.failure_rows) - len(shown_failures)
        out.append(f"<h2>Failures <span class='fail-tag'>({report.failures})</span></h2>")
        out.append("<table><thead><tr><th>when</th><th>target</th><th>probe</th><th>error</th></tr></thead><tbody>")
        for row in shown_failures:
            label = row.error or (row.error_type.value if row.error_type is not None else "failed")
            out.append(
                "<tr>"
                f"<td>{_esc(row.timestamp.isoformat())}</td>"
                f"<td><code>{_esc(row.target)}</code></td>"
                f"<td>{_esc(row.probe_type.value)}</td>"
                f"<td class='bad'>{_esc(label)}</td>"
                "</tr>"
            )
        out.append("</tbody></table>")
        if omitted_failures:
            out.append(
                f"<p class='disclosure'>… {omitted_failures} older failure(s) omitted "
                f"(newest {REPORT_MAX_FAILURES} shown; totals above cover the full window)</p>"
            )

    # Notes: the markdown's honesty section, word for word.
    out.append("<h2>Notes</h2>")
    out.append("<ul class='notes'>")
    out.append(
        "<li>Round scores are recomputed against the <em>current</em> personal baseline; the log alone "
        "cannot reconstruct the baseline as it stood at each round's time (the same honest approximation "
        "<code>netscope history</code> makes).</li>"
    )
    out.append(
        "<li><code>verdict</code> is the v0.4 baseline-relative throughput classification: within ±15% of "
        "your usual Mbps reads as <code>as_usual</code>; beyond that, <code>slower_than_usual</code> / "
        "<code>faster_than_usual</code>.</li>"
    )
    out.append("<li>Untested metrics never read as healthy: absent evidence renders as <code>--</code>, never as a passing value.</li>")
    out.append(
        "<li>Throughput evidence stays throughput evidence: verdicts live in the verdict column, latency "
        "means only ever average successful latency readings.</li>"
    )
    out.append(
        "<li>Geography rows are the place/distance the newest geo-aware measurement of that target recorded in "
        "the window &mdash; looked up at run time from the user's own GeoLite2 database, never re-looked-up here; "
        "a stale-database notice rides along when the database has aged past its refresh rhythm.</li>"
    )
    out.append("</ul>")

    out.append(
        "<footer>Generated locally by NetScope from the measurement log "
        "(<code>~/.netscope/netscope.db</code>). Nothing here is re-measured — it is what the log proves. "
        "This file is self-contained: no scripts, no external assets, nothing leaves it.</footer>"
    )
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out)
