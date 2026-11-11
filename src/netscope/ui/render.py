"""
netscope.ui.render

Rich-based terminal rendering shared by the CLI commands (and reusable
by other front ends). This layer decides *how results look* -- colors,
tables, panels -- and nothing else: every number it displays comes from
`app`/`core` already fully interpreted. One level of separation from
`explanation/explainer.py`, which owns the *wording* of a diagnosis;
this module owns the *layout* around it.

Design notes
------------
- A single shared `Console` (created lazily) so width detection, emoji
  handling, and file output behave consistently across commands.
- Level -> style mapping is the one place NetScope's color vocabulary
  lives: excellent/good green, degraded yellow, poor/down red,
  insufficient-evidence dim. JSON output never carries colors --
  machine readers get pure data (`app.reports`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from netscope.app.reports import HYPOTHESIS_SUMMARY_LABELS
from netscope.core.baseline import VERDICT_AS_USUAL, VERDICT_FASTER, VERDICT_SLOWER
from netscope.core.models import Diagnosis, ExperienceEvent, ExperienceLevel, Hypothesis, RawMeasurement, RouteHop
from netscope.core.routing import RouteChange, RouteChurnResult

_console: Console | None = None


def console() -> Console:
    """The shared rich Console (lazy so importing this module stays
    side-effect-free -- mirrors the app layer's own convention)."""
    global _console
    if _console is None:
        _console = Console(highlight=False)
    return _console


# ---------------------------------------------------------------------------
# Vocabulary: levels, severities, statuses -> styles/labels
# ---------------------------------------------------------------------------

_LEVEL_STYLES: dict[ExperienceLevel, str] = {
    ExperienceLevel.EXCELLENT: "bold green",
    ExperienceLevel.GOOD: "green",
    ExperienceLevel.DEGRADED: "yellow",
    ExperienceLevel.POOR: "red",
    ExperienceLevel.DOWN: "bold red",
}

_LEVEL_EMOJI: dict[ExperienceLevel, str] = {
    ExperienceLevel.EXCELLENT: "✓",
    ExperienceLevel.GOOD: "✓",
    ExperienceLevel.DEGRADED: "!",
    ExperienceLevel.POOR: "✗",
    ExperienceLevel.DOWN: "✗",
}

_HYPOTHESIS_STYLES: dict[Hypothesis, str] = {
    Hypothesis.LOCAL_NETWORK_ISSUE: "bold red",
    Hypothesis.ISP_ACCESS_ISSUE: "bold red",
    Hypothesis.DNS_ISSUE: "yellow",
    Hypothesis.ROUTING_DEGRADATION: "yellow",
    Hypothesis.DESTINATION_ISSUE: "yellow",
    Hypothesis.SERVICE_ISSUE: "yellow",
    Hypothesis.GENERAL_CONNECTIVITY_ISSUE: "red",
    Hypothesis.INSUFFICIENT_EVIDENCE: "dim",
}

_SEVERITY_STYLES = {"info": "dim", "warning": "yellow", "critical": "bold red"}


def level_style(level: ExperienceLevel) -> str:
    return _LEVEL_STYLES[level]


def format_score(experience: ExperienceEvent) -> Text:
    """The big one-line score: `87/100 good ✓`, colored by level."""
    style = _LEVEL_STYLES[experience.level]
    text = Text()
    text.append(f"{experience.score:.0f}", style=style)
    text.append("/100 ", style="dim")
    text.append(experience.level.value, style=style)
    text.append(f" {_LEVEL_EMOJI[experience.level]}", style=style)
    return text


def format_hypothesis(classification: Hypothesis) -> Text:
    label = HYPOTHESIS_SUMMARY_LABELS[classification]
    return Text(label, style=_HYPOTHESIS_STYLES[classification])


def format_latency(value: float | None) -> str:
    return f"{value:.1f} ms" if value is not None else "—"


def format_pct(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None else "—"


def format_time(timestamp: datetime) -> str:
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


# One line, eight levels: the smallest honest trend picture a terminal
# can draw without a charting dependency. Ordered bottom-to-top by
# visual height, so index maps directly to relative magnitude.
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float | None]) -> str:
    """A one-line Unicode trend over `values` -- the score history at a
    glance, no charting library, prints anywhere a terminal prints.

    Rules, each a honesty choice:

    - None entries (a round with nothing scorable) are skipped, not
      flattened to zero -- absence of evidence is not a low score.
    - Fewer than two scorable values is not a trend: empty string, and
      the caller shows nothing rather than a fake flat line.
    - The series normalizes to its own min..max, so a trend between 91
      and 94 is as readable as one between 20 and 95 -- a sparkline
      answers "how did it move", not "how bad is it".
    - A perfectly flat series renders the mid block repeated: nothing
      moved, and that is the whole message.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return ""
    low, high = min(clean), max(clean)
    if high == low:
        return _SPARK_BLOCKS[len(_SPARK_BLOCKS) // 2] * len(clean)
    span = high - low
    top = len(_SPARK_BLOCKS) - 1
    return "".join(_SPARK_BLOCKS[round((v - low) / span * top)] for v in clean)


# ---------------------------------------------------------------------------
# Building blocks: measurements table, score panel, diagnosis panel
# ---------------------------------------------------------------------------


def measurements_table(measurements: list[RawMeasurement], *, title: str | None = None) -> Table:
    table = Table(title=title, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Target", overflow="fold")
    table.add_column("Status", justify="center")
    table.add_column("Latency", justify="right")
    table.add_column("Loss", justify="right")
    table.add_column("Jitter", justify="right")
    table.add_column("Detail", overflow="fold", style="dim")

    for m in measurements:
        if m.success:
            status = Text("✓ ok", style="green")
        else:
            error_type = m.error_type.value if m.error_type is not None else "failed"
            status = Text(f"✗ {error_type}", style="red")
        detail = truncate(m.error) if not m.success else _success_detail(m)
        table.add_row(
            m.probe_type.value,
            m.target,
            status,
            format_latency(m.latency_ms),
            format_pct(m.packet_loss_pct),
            format_latency(m.jitter_ms),
            detail or "",
        )
    return table


def truncate(text: str | None, width: int = 64) -> str:
    """One-line, bounded detail text: probe library error strings can be
    multi-sentence (icmplib's ICMP-socket guidance especially) -- the
    table needs a hint, not an essay."""
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def format_loss_note(measurement: RawMeasurement) -> str:
    """One-line verdict for a loss-confirmation window (v0.9), shared by
    every surface that shows a measurement's detail -- the same
    one-shared-wording pattern the route memory established with
    `route_stability_summary`.

    Reads only what `core.loss.merge_loss_rounds` recorded: `loss_rounds`
    marks the measurement as a merged window, `loss_confirmed` says
    whether the loss reproduced in any sweep after the first. Empty
    string for anything else -- a plain sweep has no confirmation story
    to tell, and inventing one would be exactly the dishonesty the
    confirmation policy exists to prevent."""
    if "loss_rounds" not in measurement.extra:
        return ""
    sweeps = measurement.extra.get("loss_window_rounds", len(measurement.extra["loss_rounds"]))
    verdict = "confirmed" if measurement.extra.get("loss_confirmed") else "transient"
    return f"loss {verdict} across {sweeps} sweeps"


def _success_detail(m: RawMeasurement) -> str:
    """The probe's own non-error detail for successful measurements --
    DNS answers, HTTP status/version -- read from `extra` without
    interpreting anything (the loss note is the exception only in shape:
    it renders the verdict `core.loss` already computed, it computes
    nothing new)."""
    parts = []
    if "answers" in m.extra and isinstance(m.extra["answers"], list) and m.extra["answers"]:
        answers = ", ".join(str(r) for r in m.extra["answers"][:2])
        more = f" (+{len(m.extra['answers']) - 2})" if len(m.extra["answers"]) > 2 else ""
        parts.append(f"answers: {answers}{more}")
    if "status_code" in m.extra:
        version = f" ({m.extra['http_version']})" if "http_version" in m.extra else ""
        parts.append(f"HTTP {m.extra['status_code']}{version}")
    if "final_url" in m.extra and m.extra["final_url"] != m.target:
        parts.append(f"redirected to {m.extra['final_url']}")
    if "throughput_mbps" in m.extra:
        peak = f" · peak {m.extra['peak_mbps']:.1f}" if "peak_mbps" in m.extra else ""
        parts.append(f"{m.extra['throughput_mbps']:.1f} Mbps median{peak}")
    loss_note = format_loss_note(m)
    if loss_note:
        parts.append(loss_note)
    return " · ".join(parts)


def score_panel(experience: ExperienceEvent, *, subtitle: str | None = None) -> Panel:
    """The headline panel: big score, level, and context subtitle."""
    body = Text()
    body.append(f"{experience.score:.0f}", style=_LEVEL_STYLES[experience.level])
    body.append("/100 ", style="dim")
    body.append(experience.level.value.upper(), style=_LEVEL_STYLES[experience.level])
    body.append(f"  {_LEVEL_EMOJI[experience.level]}", style=_LEVEL_STYLES[experience.level])
    body.append("\n")
    body.append(subtitle or "internet experience right now", style="dim")
    border = {
        ExperienceLevel.EXCELLENT: "green",
        ExperienceLevel.GOOD: "green",
        ExperienceLevel.DEGRADED: "yellow",
        ExperienceLevel.POOR: "red",
        ExperienceLevel.DOWN: "red",
    }[experience.level]
    return Panel(body, border_style=border, expand=False)


_VERDICT_STYLES = {
    VERDICT_AS_USUAL: ("as usual", "dim"),
    VERDICT_SLOWER: ("slower than usual", "yellow"),
    VERDICT_FASTER: ("faster than usual", "cyan"),
}


def speed_panel(measurement: RawMeasurement) -> Panel:
    """The `netscope speed` headline panel: big median Mbps with the
    peak, bytes and window as dim evidence lines. A failed measurement
    renders its own error line -- never a fabricated number."""
    if not measurement.success:
        body = Text()
        body.append("✗ speed test failed", style="red")
        body.append("\n")
        detail = measurement.error or (measurement.error_type.value if measurement.error_type else "")
        body.append(truncate(detail), style="dim")
        return Panel(body, border_style="red", title="Download speed", expand=False)

    throughput = measurement.extra.get("throughput_mbps")
    peak = measurement.extra.get("peak_mbps")
    body = Text()
    body.append(f"{throughput:.1f}", style="bold green" if isinstance(throughput, float) else "bold")
    body.append(" Mbps", style="green")
    if isinstance(peak, float):
        body.append(f"   peak {peak:.1f}", style="dim")
    body.append("\n")
    evidence: list[str] = [measurement.target]
    bytes_transferred = measurement.extra.get("bytes_transferred")
    if isinstance(bytes_transferred, (int, float)):
        evidence.append(f"{bytes_transferred / 1e6:.1f} MB in {measurement.extra.get('duration_s', 0):.1f}s")
    sample_count = measurement.extra.get("sample_count")
    if isinstance(sample_count, (int, float)) and sample_count:
        evidence.append(f"{int(sample_count)} samples")
    body.append(" · ".join(evidence), style="dim")
    verdict = measurement.extra.get("verdict")
    usual = measurement.extra.get("baseline_mean_mbps")
    deviation = measurement.extra.get("deviation_pct")
    if verdict is not None and isinstance(usual, (int, float)) and isinstance(deviation, (int, float)):
        label, style = _VERDICT_STYLES.get(verdict, (str(verdict), "dim"))
        body.append("\n")
        body.append(f"vs your usual {usual:.1f} Mbps · {deviation:+.1f}% — {label}", style=style)
    return Panel(body, border_style="green", title="Download speed", expand=False)


def diagnosis_panel(diagnosis: Diagnosis | None) -> Panel:
    """The diagnosis verdict panel. `None` (the confirmed-healthy
    convention) renders its own positive line rather than being
    indistinguishable from 'not run'."""
    if diagnosis is None:
        body = Text("No issues detected.", style="green")
        body.append("\nall checked paths healthy", style="dim")
        return Panel(body, title="Diagnosis", border_style="green", expand=False)

    lines = Text()
    label = HYPOTHESIS_SUMMARY_LABELS[diagnosis.classification]
    lines.append(label, style=_HYPOTHESIS_STYLES[diagnosis.classification])
    if diagnosis.classification is Hypothesis.INSUFFICIENT_EVIDENCE:
        lines.append("  (confidence: —)", style="dim")
    else:
        lines.append(f"  (confidence: {diagnosis.confidence * 100:.0f}%)", style="dim")

    problems = [e for e in diagnosis.evidence if e.tested and e.severity.value != "info"]
    if problems:
        lines.append("\n")
        lines.append("Evidence:", style="bold")
        for e in problems:
            lines.append(f"\n  • {e.metric}", style=_SEVERITY_STYLES[e.severity.value])
            if e.observed_value is not None:
                lines.append(f": observed {e.observed_value:.1f}")
            if e.expected_value is not None:
                lines.append(f" (expected ~{e.expected_value:.1f})")
            if e.deviation is not None and e.deviation > 0:
                lines.append(f" — {e.deviation:.1f}σ above your normal")

    return Panel(lines, title="Diagnosis", border_style=_HYPOTHESIS_STYLES[diagnosis.classification], expand=False)


# ---------------------------------------------------------------------------
# Route rendering (shared by `netscope route` and the TUI's route view)
# ---------------------------------------------------------------------------


def route_table(hops: list[RouteHop], *, title: str | None = None) -> Table:
    table = Table(title=title, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Host", overflow="fold")
    table.add_column("Address", overflow="fold")
    table.add_column("ASN", style="magenta", no_wrap=True)
    table.add_column("RTT", justify="right")
    table.add_column("Loss", justify="right")

    for hop in hops:
        rtt = f"{hop.avg_rtt_ms:.1f} ms" if hop.avg_rtt_ms is not None else Text("no response", style="dim")
        loss = format_pct(hop.packet_loss_pct) if hop.packet_loss_pct else "—"
        table.add_row(
            str(hop.ttl),
            hop.hostname or "—",
            hop.address or Text("*", style="dim"),
            hop.asn or "—",
            rtt,
            loss,
        )
    return table


def format_route_change(change: RouteChange) -> str:
    """One RouteChange -> a human one-liner naming WHICH hops moved:
    'hop 3: 10.0.0.3 → no reply, hop 4: 10.0.0.4 → 10.0.0.9 (+2 more)'.

    Presentation is the one place allowed to know how a hop diff reads
    -- core.routing only reports the data (v0.8's RouteHopChange list).
    A dropped hop reads as '→ no reply' (the traceroute truth: that
    position stopped answering, '*' in the signature); an added hop is
    the reverse. At most three hops are spelled out before a '+N more'
    keeps the line one line -- the full diff stays in the data for the
    --json payload."""
    if not change.changed_hops:
        return "path changed"
    parts = []
    for hop_change in change.changed_hops[:3]:
        previous = hop_change.previous_address or "no reply"
        new = hop_change.new_address or "no reply"
        parts.append(f"hop {hop_change.ttl}: {previous} → {new}")
    line = ", ".join(parts)
    if len(change.changed_hops) > 3:
        line += f" (+{len(change.changed_hops) - 3} more)"
    return line


def route_stability_summary(churn: RouteChurnResult, prior_runs: int, *, compact: bool = False) -> str:
    """The churn verdict as one human phrase -- the shared wording behind
    `netscope route`'s stability line and the TUI route screen's status
    tail. `compact=True` is the TUI's shorter density (a tight time
    stamp, no 'since' clause); the verdict logic lives in exactly one
    place so the two views cannot drift.

    The honesty rule mirrors the use case's: `prior_runs == 0` is a
    FIRST OBSERVATION -- 'stable' would claim a certainty (nothing has
    changed) the evidence does not support, so it is never printed for
    a single snapshot."""
    if prior_runs == 0:
        return "first observation" if compact else "first observation — future runs compare against this path"
    if churn.is_stable:
        if compact:
            return f"stable across {churn.snapshot_count} runs"
        since = churn.window_start
        suffix = f" — same path since {format_time(since)}" if since is not None else ""
        return f"unchanged across the last {churn.snapshot_count} runs{suffix}"
    change = churn.last_change
    if change is None:
        return "path changed"
    diff = format_route_change(change)
    if compact:
        return f"changed vs {change.previous_timestamp.strftime('%m-%d %H:%M')} — {diff}"
    return f"changed vs {format_time(change.previous_timestamp)} — {diff}"
