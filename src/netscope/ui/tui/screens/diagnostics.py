"""
netscope.ui.tui.screens.diagnostics

The diagnostics screen (TASK-044): the full evidence view behind the
dashboard's one-line summary. Where the dashboard compresses a round
into a score and a verdict, this screen lays the reasoning out --
every evidence row (metric, observed, expected, deviation, severity),
what was ruled out, and the plain-language explanation NetScope
generates from the same diagnosis.

WHY IT READS FROM THE SHELL, NOT THE LOG
----------------------------------------
The diagnosis comes from the shell's latest round (delivered through
`on_new_round`), which is the SAME diagnosis the dashboard showed and
the same one `netscope diagnose` would produce -- one measurement
pipeline, one evidence engine, three views. This screen runs no probes
of its own; `r` asks the shell for a fresh round.

A round with no diagnosis renders as healthy -- honestly: "all checked
paths healthy", not an empty panel pretending data is coming.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from netscope.app.monitoring import IncidentTransition
from netscope.app.reports import HYPOTHESIS_SUMMARY_LABELS
from netscope.core.models import Diagnosis, ExperienceEvent, RawMeasurement
from netscope.explanation.explainer import explain
from netscope.ui.tui.screens import LEVEL_COLORS

if TYPE_CHECKING:
    from netscope.ui.tui.app import NetScopeTUI


class DiagnosticsScreen(Screen):
    """The evidence behind the verdict: rows, ruled-outs, explanation."""

    BINDINGS = [
        Binding("r", "round", "Run round now"),
    ]

    AUTO_FOCUS = None

    CSS = """
    #diag_headline {
        height: 5;
        padding: 0 1;
        border: round $panel;
    }
    #evidence_table {
        width: 1fr;
        height: 1fr;
        border: round $panel;
    }
    #explanation_log {
        width: 48;
        height: 1fr;
        border: round $panel;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        # The shell re-delivers the latest round the moment this screen
        # becomes active -- which can beat composition. Rounds arriving
        # early are replayed on mount, never dropped.
        self._mounted = False
        self._pending_round: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("[dim]no round yet — r runs one now[/dim]", id="diag_headline", markup=True)
        with Horizontal():
            yield DataTable(id="evidence_table")
            yield RichLog(id="explanation_log", markup=True, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self._mounted = True
        table = self.query_one("#evidence_table", DataTable)
        table.add_columns("Metric", "Tested", "Observed", "Expected", "Deviation", "Severity")

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
        """Repaint the whole evidence view from the shell's latest
        round."""
        if not self._mounted:
            self._pending_round = (measurements, experience, diagnosis, transition)
            return
        self._update_headline(experience, diagnosis)
        self._update_evidence_table(diagnosis)
        self._update_explanation(diagnosis)

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

    def _update_headline(self, experience: ExperienceEvent, diagnosis: Diagnosis | None) -> None:
        color = LEVEL_COLORS[experience.level]
        if diagnosis is None:
            verdict = "[bold green]all checked paths healthy[/]"
        else:
            label = HYPOTHESIS_SUMMARY_LABELS[diagnosis.classification]
            verdict = f"[bold red]{label}[/] [dim](confidence: {diagnosis.confidence * 100:.0f}%)[/]"
        self.query_one("#diag_headline", Static).update(
            f"[bold {color}]{experience.score:.0f}[/][dim]/100 · {experience.level.value.upper()}[/]"
            f" · round at {experience.timestamp.strftime('%H:%M:%S')}\n{verdict}"
        )

    def _update_evidence_table(self, diagnosis: Diagnosis | None) -> None:
        table = self.query_one("#evidence_table", DataTable)
        table.clear()
        if diagnosis is None:
            return
        for e in diagnosis.evidence:
            tested = "[green]yes[/]" if e.tested else "[dim]no[/]"
            severity = e.severity.value
            severity_cell = (
                f"[red]{severity}[/]" if severity == "critical" else (f"[yellow]{severity}[/]" if severity == "warning" else f"[dim]{severity}[/]")
            )
            table.add_row(
                e.metric,
                tested,
                f"{e.observed_value:.1f}" if e.observed_value is not None else "—",
                f"~{e.expected_value:.1f}" if e.expected_value is not None else "—",
                f"{e.deviation:+.1f}" if e.deviation is not None else "—",
                severity_cell,
            )

    def _update_explanation(self, diagnosis: Diagnosis | None) -> None:
        log = self.query_one("#explanation_log", RichLog)
        log.clear()
        if diagnosis is None:
            log.write("[bold green]No issues detected.[/]")
            log.write("[dim]every evidence row tested healthy, so there is nothing to explain.[/]")
            return
        if diagnosis.classification.value == "insufficient_evidence":
            log.write("[yellow]Not enough evidence to name a cause yet.[/]")
        for line in explain(diagnosis).splitlines():
            log.write(line)
