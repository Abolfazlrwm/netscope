"""
netscope.ui.tui.screens.services

The services screen (TASK-046): the status board of every target
NetScope has actually measured -- the round's built-in targets
(gateway, public DNS, the lookup domain, the CDN) plus every host the
user ever checked -- one row per target, one cell per service check,
showing each check's LATEST persisted result. Plus on-demand checks:
type a host, enter, and its five checks run, persist, and join the
board.

WHAT "SERVICES" MEANS HERE
--------------------------
TASK-032's Service model and TASK-033's `run_service_checks` exist;
what deliberately does not exist yet is a place to PERSIST a user's
curated service list (the roadmap keeps TASK-032's storage half open).
Inventing a hidden service registry now would fake persistence the
schema doesn't have. So the board derives its rows from the
measurement log itself: every target the log holds is a service
someone asked NetScope about. An unchecked (target, check) cell
renders as a dash -- absent evidence shown as absent, never as an
"unknown" state.

On-demand checks go through `check_target` -- the same use case behind
`netscope check`, scoring included -- and persist exactly like the CLI
does (save-when-repository-wired), so the board and the CLI stay one
view of one log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, RichLog

from netscope.app.use_cases import check_target
from netscope.core.models import SERVICE_CHECK_TYPES, ProbeType, RawMeasurement

if TYPE_CHECKING:
    from netscope.ui.tui.app import NetScopeTUI

# Fixed display order for the check columns.
_CHECK_COLUMNS: tuple[ProbeType, ...] = (ProbeType.ICMP, ProbeType.DNS, ProbeType.TCP, ProbeType.TLS, ProbeType.HTTP)


class ServicesLoaded(Message):
    """One log read for the status board, posted from the worker
    thread."""

    def __init__(self, rows: list[RawMeasurement]) -> None:
        super().__init__()
        self.rows = rows


class CheckDone(Message):
    """One on-demand check finished (and persisted, when a repository
    is wired)."""

    def __init__(self, target: str, measurements: list[RawMeasurement], score: float, level_value: str) -> None:
        super().__init__()
        self.target = target
        self.measurements = measurements
        self.score = score
        self.level_value = level_value


class ServicesScreen(Screen):
    """The per-target service status board, with on-demand checks."""

    BINDINGS = [
        Binding("r", "reload", "Reload from log"),
    ]

    AUTO_FOCUS = None

    CSS = """
    #service_input {
        margin: 0 1;
    }
    #services_table {
        height: 1fr;
        border: round $panel;
    }
    #check_log {
        height: 9;
        border: round $panel;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Input(placeholder="host to check now — enter runs icmp,dns,tcp,tls,http", id="service_input")
            yield DataTable(id="services_table")
            yield RichLog(id="check_log", markup=True, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#services_table", DataTable)
        table.add_columns("Target", *[pt.value.upper() for pt in _CHECK_COLUMNS], "Last seen")
        target_input = self.query_one("#service_input", Input)
        target_input.blur()  # screen keys first; tab focuses the input
        self.action_reload()

    # ------------------------------------------------------------------
    # Log reading (background worker -> ServicesLoaded message)
    # ------------------------------------------------------------------

    def action_reload(self) -> None:
        self._run_load_worker()

    @work(thread=True, exclusive=True, group="services")
    def _run_load_worker(self) -> None:
        shell = cast("NetScopeTUI", self.app)
        repo = shell.container.measurement_repository
        rows = repo.latest(limit=500) if repo is not None else []
        self.post_message(ServicesLoaded(rows=rows))

    def on_services_loaded(self, message: ServicesLoaded) -> None:
        self._fill_table(message.rows)

    # ------------------------------------------------------------------
    # On-demand checks (background worker -> CheckDone message)
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "service_input":
            return
        target = event.value.strip()
        if not target:
            return
        event.input.blur()
        log = self.query_one("#check_log", RichLog)
        log.write(f"[dim]checking {target}…[/dim]")
        self._run_check_worker(target)

    @work(thread=True, exclusive=True, group="check")
    def _run_check_worker(self, target: str) -> None:
        shell = cast("NetScopeTUI", self.app)
        measurements, experience, _diagnosis = check_target(target, shell.container, SERVICE_CHECK_TYPES)
        repo = shell.container.measurement_repository
        if repo is not None:
            for measurement in measurements:
                repo.save(measurement)
        self.post_message(CheckDone(target=target, measurements=measurements, score=experience.score, level_value=experience.level.value))

    def on_check_done(self, message: CheckDone) -> None:
        log = self.query_one("#check_log", RichLog)
        log.write(
            f"[bold]{message.target}[/]: score [bold]{message.score:.0f}[/]/100 ({message.level_value})"
            f" · [dim]persisted where a log is wired[/]"
        )
        for m in message.measurements:
            if m.success:
                latency = f"{m.latency_ms:.1f} ms" if m.latency_ms is not None else ""
                log.write(f"  [green]✓[/] {m.probe_type.value} {m.target} [dim]{latency}[/]")
            else:
                error = " ".join((m.error or "").split())[:60]
                log.write(f"  [red]✗[/] {m.probe_type.value} {m.target} [dim]{error}[/]")
        self.action_reload()  # the board now includes the new target

    # ------------------------------------------------------------------
    # Rendering helpers (UI thread only)
    # ------------------------------------------------------------------

    def _fill_table(self, rows: list[RawMeasurement]) -> None:
        table = self.query_one("#services_table", DataTable)
        table.clear()

        latest: dict[tuple[str, ProbeType], RawMeasurement] = {}
        last_seen: dict[str, RawMeasurement] = {}
        for m in rows:
            if m.target not in last_seen or m.timestamp > last_seen[m.target].timestamp:
                last_seen[m.target] = m
            if m.probe_type not in SERVICE_CHECK_TYPES:
                continue  # bandwidth/traceroute rows are not service-check evidence
            key = (m.target, m.probe_type)
            current = latest.get(key)
            if current is None or m.timestamp > current.timestamp:
                latest[key] = m

        if not last_seen:
            self.query_one("#check_log", RichLog).write(
                "[yellow]no measurements logged yet[/] [dim]— run netscope diagnose, or check a host above[/]"
            )
            return

        for target in sorted(last_seen):
            cells: list[str] = []
            for pt in _CHECK_COLUMNS:
                cells.append(self._status_cell(latest.get((target, pt))))
            seen = last_seen[target].timestamp.strftime("%m-%d %H:%M")
            table.add_row(target, *cells, f"[dim]{seen}[/]")

    @staticmethod
    def _status_cell(measurement: RawMeasurement | None) -> str:
        if measurement is None:
            return "[dim]—[/]"
        if measurement.success:
            if measurement.latency_ms is not None:
                return f"[green]✓ {measurement.latency_ms:.1f}[/]"
            return "[green]✓[/]"
        reason = measurement.error_type.value if measurement.error_type is not None else "failed"
        return f"[red]✗ {reason}[/]"
