"""
netscope.ui.tui.app

NetScope's live terminal dashboard (Textual) — `netscope tui`.

WHAT IT SHOWS
-------------
- The current experience score (0-100), a sparkline of recent scores,
  every probe result of the latest round, the evidence-based
  diagnosis, incident transitions, and the local network interfaces.
- `r` forces an immediate round, `q` quits; rounds run automatically
  every `interval` seconds.

HOW IT RUNS
-----------
Rounds execute on background workers (via `run_measurement_round`, the
same use case the CLI uses -- never a second pipeline) and post their
*results* back to the UI thread as a message. Everything degrades
honestly: failed probes render as failed; a failed round logs the
error; the network panel is best-effort decoration.

TESTABILITY
-----------
`NetScopeTUI` accepts an injected `container`, `config`, and
`round_fn`, so tests drive it fully offline with fake probes and zero
wall-clock timing (set `interval` large and call `request_round()`
directly).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Footer, Header, Label, RichLog, Sparkline, Static

from netscope.app.config import NetScopeConfig
from netscope.app.container import Container, build_container
from netscope.app.gateway import resolve_gateway
from netscope.app.monitoring import MonitorSession, TransitionType
from netscope.app.reports import HYPOTHESIS_SUMMARY_LABELS
from netscope.app.use_cases import run_measurement_round
from netscope.core.models import Diagnosis, ExperienceEvent, ExperienceLevel, RawMeasurement

RoundFn = Callable[[Container, NetScopeConfig, str | None], tuple]

_LEVEL_COLORS: dict[ExperienceLevel, str] = {
    ExperienceLevel.EXCELLENT: "green",
    ExperienceLevel.GOOD: "green",
    ExperienceLevel.DEGRADED: "yellow",
    ExperienceLevel.POOR: "red",
    ExperienceLevel.DOWN: "red",
}


@dataclass
class RoundDone(Message):
    """One completed measurement round, posted from the worker thread
    to the UI thread (post_message is thread-safe)."""

    measurements: list[RawMeasurement] = field(default_factory=list)
    experience: ExperienceEvent | None = None
    diagnosis: Diagnosis | None = None


class NetScopeTUI(App):
    """The live NetScope dashboard."""

    TITLE = "NetScope"
    SUB_TITLE = "internet experience intelligence"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
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

    def __init__(
        self,
        container: Container | None = None,
        config: NetScopeConfig | None = None,
        *,
        interval: float = 15.0,
        round_fn: RoundFn | None = None,
    ):
        super().__init__()
        self._container = container
        self._config = config
        self._interval = interval
        self._round_fn = round_fn
        self._gateway: str | None = None
        self._score_history: list[float] = []
        self._round_count = 0
        self._session = MonitorSession()

    # ------------------------------------------------------------------
    # Wiring properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> NetScopeConfig:
        return self._config if self._config is not None else NetScopeConfig()

    @property
    def container(self) -> Container:
        if self._container is None:
            self._container = build_container()
        return self._container

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
        checks = self.query_one("#checks_table", DataTable)
        checks.add_columns("Check", "Target", "Status", "Latency", "Loss", "Detail")

        self.query_one("#network_card", Static).update(self._network_text())

        self._gateway = resolve_gateway(None, self.config)
        self._log_line(f"[dim]every {self._interval:g}s · gateway: {self._gateway or 'not detected'} · r: run now · q: quit[/dim]")

        self.set_interval(self._interval, self.request_round)
        self.request_round()

    # ------------------------------------------------------------------
    # Round execution (background worker -> RoundDone message)
    # ------------------------------------------------------------------

    def request_round(self) -> None:
        """Schedule one measurement round on a background worker."""
        self._run_round_worker()

    @work(thread=True, exclusive=True, group="round")
    def _run_round_worker(self) -> None:
        container = self.container
        config = self.config
        round_fn: RoundFn = self._round_fn or run_measurement_round
        try:
            measurements, experience, diagnosis = round_fn(container, config, self._gateway)
        except Exception as exc:  # noqa: BLE001 -- a failed round is reported, never fatal
            self.call_from_thread(self._log_line, f"[red]round failed: {exc}[/red]")
            return
        self.post_message(RoundDone(measurements=measurements, experience=experience, diagnosis=diagnosis))

    def on_round_done(self, message: RoundDone) -> None:
        assert message.experience is not None
        self._round_count += 1
        self._score_history.append(message.experience.score)
        self._score_history = self._score_history[-60:]

        self._update_score_card(message.experience)
        self._update_checks_table(message.measurements)
        self._update_diagnosis(message.diagnosis)
        self._update_trend()

        transition = self._session.process(message.diagnosis, message.experience.timestamp)
        if transition.kind is TransitionType.OPENED and transition.incident is not None:
            label = HYPOTHESIS_SUMMARY_LABELS[transition.incident.diagnosis.classification] if transition.incident.diagnosis else "problem sustained"
            self._log_line(f"[bold red]⚠ incident OPENED — {label}[/bold red]")
        elif transition.kind is TransitionType.CLOSED and transition.incident is not None:
            self._log_line("[green]✓ incident CLOSED — sustained healthy again[/green]")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_round(self) -> None:
        self._log_line("[dim]running round now…[/dim]")
        self.request_round()

    # ------------------------------------------------------------------
    # Rendering helpers (UI thread only)
    # ------------------------------------------------------------------

    def _update_score_card(self, experience: ExperienceEvent) -> None:
        color = _LEVEL_COLORS[experience.level]
        self.query_one("#score_card", Static).update(
            f"[bold {color}]  {experience.score:.0f}[/] [dim]/100[/]\n"
            f"  [bold {color}]{experience.level.value.upper()}[/]\n"
            f"  [dim]round #{self._round_count} · {experience.timestamp.strftime('%H:%M:%S')}[/]"
        )

    def _update_trend(self) -> None:
        spark = self.query_one("#trend_spark", Sparkline)
        spark.data = self._score_history if len(self._score_history) >= 2 else []

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
            snapshot = self.container.discovery_provider.discover()
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
        stamp = datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{stamp}[/] {text}")
