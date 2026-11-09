"""
netscope.ui.tui.screens.dashboard

The dashboard screen (TASK-041) -- the original v0.2 live view, now one
screen among six: the current experience score (0-100), a sparkline of
recent scores, every probe result of the latest round, the evidence-
based diagnosis, incident transitions, and the local network
interfaces.

WHAT CHANGED IN v0.6
--------------------
Nothing about WHAT this screen shows -- only WHO owns the plumbing.
Round execution, the incident session, and dependency wiring moved up
to the shell (`ui/tui/app.py`); this screen receives each round through
`on_new_round(...)`, which the shell calls when a round lands AND when
the screen becomes active again -- switching to the route screen and
back never leaves the dashboard staring at a stale round.

`r` forces an immediate round (through the shell's worker -- this
screen never runs a probe itself); rounds otherwise arrive on the
shell's interval. Everything degrades honestly: failed probes render as
failed, a failed round is logged as an error, the network panel is
best-effort decoration.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, RichLog, Sparkline, Static

from netscope.app.monitoring import IncidentTransition, TransitionType
from netscope.app.reports import HYPOTHESIS_SUMMARY_LABELS
from netscope.core.models import Diagnosis, ExperienceEvent, RawMeasurement
from netscope.ui.tui.screens import LEVEL_COLORS

if TYPE_CHECKING:
    from netscope.ui.tui.app import NetScopeTUI


class DashboardScreen(Screen):
    """The live overview: score, trend, per-probe results, diagnosis,
    incidents, network interfaces."""

    BINDINGS = [
        Binding("r", "round", "Run round now"),
    ]

    CSS = """
    #score_card {
        width: 36;
        height: 7;
        border: round $success;
        padding: 0 1;
    }
    #trend_card {
        width: 1fr;
        height: 7;
        border: round $panel;
        padding: 0 1;
    }
    #trend_label {
        color: $text-muted;
    }
    #checks_table {
        height: 1fr;
        border: round $panel;
    }
    #diagnosis_card {
        width: 1fr;
        height: 10;
        border: round $panel;
    }
    #network_card {
        width: 46;
        height: 10;
        border: round $panel;
        padding: 0 1;
    }
    #log {
        height: 8;
        border: round $panel;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        # A round worker can fail (and report that failure) before this
        # screen has finished composing -- early lines are buffered and
        # flushed on mount rather than lost. The same holds for whole
        # rounds: one may land between the shell's mount and this
        # screen's, and is replayed instead of dropped.
        self._early_lines: list[str] = []
        self._mounted = False
        self._pending_round: tuple | None = None

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield Static("waiting for first round…", id="score_card", markup=True)
            with Vertical(id="trend_card"):
                yield Label("score trend — recent rounds", id="trend_label")
                yield Sparkline([], id="trend_spark", summary_function=max)
        yield DataTable(id="checks_table")
        with Horizontal():
            yield RichLog(id="diagnosis_card", markup=True, wrap=True, highlight=False)
            yield Static("", id="network_card", markup=True)
        yield RichLog(id="log", markup=True, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self._mounted = True
        checks = self.query_one("#checks_table", DataTable)
        checks.add_columns("Check", "Target", "Status", "Latency", "Loss", "Detail")

        self.query_one("#network_card", Static).update(self._network_text())

        shell = cast("NetScopeTUI", self.app)
        self._log_line(
            f"[dim]every {shell.interval:g}s · gateway: {shell.gateway or 'not detected'} · keys: d dashboard · r round now · q quit[/dim]"
        )

        for early in self._early_lines:
            self._log_line(early)
        self._early_lines.clear()

        if self._pending_round is not None:
            pending, self._pending_round = self._pending_round, None
            self.on_new_round(*pending)

    # ------------------------------------------------------------------
    # Live data (delivered by the shell)
    # ------------------------------------------------------------------

    def on_new_round(
        self,
        measurements: list[RawMeasurement],
        experience: ExperienceEvent,
        diagnosis: Diagnosis | None,
        transition: IncidentTransition,
    ) -> None:
        """One round arrived (or this screen just became active with a
        round already on file) -- repaint every panel from it."""
        if not self._mounted:
            # Compose hasn't finished; replay once the columns exist.
            self._pending_round = (measurements, experience, diagnosis, transition)
            return
        self._update_score_card(experience)
        self._update_checks_table(measurements)
        self._update_diagnosis(diagnosis)
        self._update_trend()

        if transition.kind is TransitionType.OPENED and transition.incident is not None:
            label = HYPOTHESIS_SUMMARY_LABELS[transition.incident.diagnosis.classification] if transition.incident.diagnosis else "problem sustained"
            self._log_line(f"[bold red]⚠ incident OPENED — {label}[/bold red]")
        elif transition.kind is TransitionType.CLOSED and transition.incident is not None:
            self._log_line("[green]✓ incident CLOSED — sustained healthy again[/green]")
        elif transition.kind is TransitionType.UPDATED and transition.incident is not None:
            self._log_line("[yellow]• incident ongoing — evidence accumulating[/yellow]")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_round(self) -> None:
        shell = cast("NetScopeTUI", self.app)
        shell.log_line("[dim]running round now…[/dim]")
        shell.request_round()

    # ------------------------------------------------------------------
    # Rendering helpers (UI thread only)
    # ------------------------------------------------------------------

    def _update_score_card(self, experience: ExperienceEvent) -> None:
        shell = cast("NetScopeTUI", self.app)
        color = LEVEL_COLORS[experience.level]
        self.query_one("#score_card", Static).update(
            f"[bold {color}]  {experience.score:.0f}[/] [dim]/100[/]\n"
            f"  [bold {color}]{experience.level.value.upper()}[/]\n"
            f"  [dim]round #{shell.round_count} · {experience.timestamp.strftime('%H:%M:%S')}[/]"
        )

    def _update_trend(self) -> None:
        spark = self.query_one("#trend_spark", Sparkline)
        history = cast("NetScopeTUI", self.app).score_history
        spark.data = history if len(history) >= 2 else []

    def _update_checks_table(self, measurements: list[RawMeasurement]) -> None:
        table = self.query_one("#checks_table", DataTable)
        table.clear()
        for m in measurements:
            if m.success:
                status = "[green]✓ ok[/]"
                if "status_code" in m.extra:
                    detail = f"HTTP {m.extra['status_code']}"
                elif "answers" in m.extra and isinstance(m.extra["answers"], list) and m.extra["answers"]:
                    detail = f"answers: {', '.join(str(a) for a in m.extra['answers'][:2])}"
                else:
                    detail = ""
            else:
                error_type = m.error_type.value if m.error_type is not None else "failed"
                status = f"[red]✗ {error_type}[/]"
                detail = " ".join((m.error or "").split())[:48]
            table.add_row(
                m.probe_type.value,
                m.target,
                status,
                f"{m.latency_ms:.1f} ms" if m.latency_ms is not None else "—",
                f"{m.packet_loss_pct:.0f}%" if m.packet_loss_pct is not None else "—",
                detail,
            )

    def _update_diagnosis(self, diagnosis: Diagnosis | None) -> None:
        log = self.query_one("#diagnosis_card", RichLog)
        log.clear()
        if diagnosis is None:
            log.write("[bold green]No issues detected.[/]")
            log.write("[dim]all checked paths healthy[/]")
            return
        label = HYPOTHESIS_SUMMARY_LABELS[diagnosis.classification]
        if diagnosis.classification.value == "insufficient_evidence":
            log.write(f"[yellow]{label}[/]")
        else:
            log.write(f"[bold red]{label}[/]  [dim](confidence: {diagnosis.confidence * 100:.0f}%)[/]")
        problems = [e for e in diagnosis.evidence if e.tested and e.severity.value != "info"]
        for e in problems[:6]:
            line = f"  • {e.metric}"
            if e.observed_value is not None:
                line += f": observed {e.observed_value:.1f}"
            if e.expected_value is not None:
                line += f" (expected ~{e.expected_value:.1f})"
            color = "red" if e.severity.value == "critical" else "yellow"
            log.write(f"[{color}]{line}[/]")

    def _network_text(self) -> str:
        try:
            snapshot = cast("NetScopeTUI", self.app).container.discovery_provider.discover()
        except Exception:  # noqa: BLE001 -- discovery is best-effort decoration
            return "[bold]Network interfaces[/]\n[dim]unavailable[/]"
        lines = ["[bold]Network interfaces[/]"]
        for iface in snapshot.interfaces:
            if iface.is_loopback:
                continue
            addrs = ", ".join(iface.addresses[:2]) or "—"
            lines.append(f"  {'●' if iface.is_up else '○'} {iface.name} [dim]({iface.network_type.value})[/] {addrs}")
        body = "\n".join(lines[:8])
        return body if len(lines) > 1 else "[bold]Network interfaces[/]\n[dim]no non-loopback interfaces[/]"

    def _log_line(self, text: str) -> None:
        try:
            log = self.query_one("#log", RichLog)
        except NoMatches:
            # Compose hasn't finished yet -- buffer for the flush on mount.
            self._early_lines.append(text)
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        log.write(f"[dim]{stamp}[/] {text}")
