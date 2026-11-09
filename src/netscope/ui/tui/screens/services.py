"""
netscope.ui.tui.screens.services

The services screen (TASK-046; v0.7 closes TASK-032's storage half):
the status board of every service NetScope knows about -- the user's
SAVED services (the curated list persisted in SQLite since v0.7) pinned
at the top, plus every target the measurement log holds. One row per
target, one cell per service check, showing each check's LATEST
persisted result. Plus on-demand checks and saves: type a host to check
it now, or type `name host` to save a service to the board.

WHAT "SERVICES" MEANS HERE (and what v0.7 changed)
----------------------------------------------------------
TASK-032's Service model and TASK-033's `run_service_checks` exist; what
v0.6 deliberately did NOT have was a place to PERSIST a user's curated
service list (the roadmap kept TASK-032's storage half open), so the
board derived every row from the measurement log itself. v0.7 closes
that half (`ServiceRepository` port + the `services` table, schema v3),
and the board now shows the truth of both sources:

- a SAVED service appears pinned (cyan, starred) even before its first
  check -- dashes for never-measured cells, "never" for last-seen. That
  is the whole point of the list: a service you care about is on the
  board before it has any evidence, not only after.
- every target the log holds that is not saved keeps its own row,
  unchanged from v0.6.
- a saved row is keyed by the service's HOST (that is what gets
  measured), with the curated name as the label; a host that is both
  saved and logged shows once -- the pinned row.

Saving from the board goes through `save_service` (the same use case
behind `netscope service add`, all five checks enabled) and fails
honestly when no ServiceRepository is wired. Removal is deliberately
CLI-only (`netscope service remove`): the board has no destructive
keys.

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

from netscope.app.use_cases import check_target, save_service
from netscope.core.models import SERVICE_CHECK_TYPES, ProbeType, RawMeasurement, Service

if TYPE_CHECKING:
    from netscope.ui.tui.app import NetScopeTUI

# Fixed display order for the check columns.
_CHECK_COLUMNS: tuple[ProbeType, ...] = (ProbeType.ICMP, ProbeType.DNS, ProbeType.TCP, ProbeType.TLS, ProbeType.HTTP)


class ServicesLoaded(Message):
    """One log read + one saved-list read for the status board, posted
    from the worker thread."""

    def __init__(self, rows: list[RawMeasurement], services: list[Service]) -> None:
        super().__init__()
        self.rows = rows
        self.services = services


class CheckDone(Message):
    """One on-demand check finished (and persisted, when a repository
    is wired)."""

    def __init__(self, target: str, measurements: list[RawMeasurement], score: float, level_value: str) -> None:
        super().__init__()
        self.target = target
        self.measurements = measurements
        self.score = score
        self.level_value = level_value


class ServiceSaved(Message):
    """One service saved to the curated list (posted from the worker
    thread; `created` False means an existing entry was updated)."""

    def __init__(self, name: str, created: bool) -> None:
        super().__init__()
        self.name = name
        self.created = created


class ServiceSaveFailed(Message):
    """One save attempt failed -- the use case's own honest message
    (a blank name/host, invalid checks, or no ServiceRepository wired),
    never a silent drop."""

    def __init__(self, detail: str) -> None:
        super().__init__()
        self.detail = detail


class ServicesScreen(Screen):
    """The per-target service status board, with on-demand checks and
    saves."""

    BINDINGS = [
        Binding("r", "reload", "Reload board"),
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
            yield Input(placeholder="host to check now — or 'name host' to save a service (host is the last word)", id="service_input")
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
    # Board loading (background worker -> ServicesLoaded message)
    # ------------------------------------------------------------------

    def action_reload(self) -> None:
        self._run_load_worker()

    @work(thread=True, exclusive=True, group="services")
    def _run_load_worker(self) -> None:
        shell = cast("NetScopeTUI", self.app)
        repo = shell.container.measurement_repository
        rows = repo.latest(limit=500) if repo is not None else []
        service_repo = shell.container.service_repository
        services = service_repo.load_all() if service_repo is not None else []
        self.post_message(ServicesLoaded(rows=rows, services=services))

    def on_services_loaded(self, message: ServicesLoaded) -> None:
        self._fill_table(message.rows, message.services)

    # ------------------------------------------------------------------
    # On-demand checks and saves (background workers -> messages)
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "service_input":
            return
        text = event.value.strip()
        if not text:
            return
        event.input.blur()
        log = self.query_one("#check_log", RichLog)
        tokens = text.split()
        if len(tokens) == 1:
            log.write(f"[dim]checking {text}…[/dim]")
            self._run_check_worker(text)
        else:
            # The host is the last word (hosts never contain spaces),
            # everything before it is the curated name -- so 'My VPN
            # vpn.example.net' saves as name 'My VPN'. Same use case as
            # `netscope service add`, all five checks.
            host = tokens[-1]
            name = " ".join(tokens[:-1])
            log.write(f"[dim]saving service '{name}' for {host}…[/dim]")
            self._run_save_worker(name, host)

    @work(thread=True, exclusive=True, group="check")
    def _run_check_worker(self, target: str) -> None:
        shell = cast("NetScopeTUI", self.app)
        measurements, experience, _diagnosis = check_target(target, shell.container, SERVICE_CHECK_TYPES)
        repo = shell.container.measurement_repository
        if repo is not None:
            for measurement in measurements:
                repo.save(measurement)
        self.post_message(CheckDone(target=target, measurements=measurements, score=experience.score, level_value=experience.level.value))

    @work(thread=True, exclusive=True, group="save")
    def _run_save_worker(self, name: str, host: str) -> None:
        shell = cast("NetScopeTUI", self.app)
        try:
            _service, created = save_service(name, host, SERVICE_CHECK_TYPES, shell.container)
        except (ValueError, RuntimeError) as exc:
            self.post_message(ServiceSaveFailed(detail=str(exc)))
            return
        self.post_message(ServiceSaved(name=name, created=created))

    def on_check_done(self, message: CheckDone) -> None:
        log = self.query_one("#check_log", RichLog)
        log.write(
            f"[bold]{message.target}[/]: score [bold]{message.score:.0f}[/]/100 ({message.level_value}) · [dim]persisted where a log is wired[/]"
        )
        for m in message.measurements:
            if m.success:
                latency = f"{m.latency_ms:.1f} ms" if m.latency_ms is not None else ""
                log.write(f"  [green]✓[/] {m.probe_type.value} {m.target} [dim]{latency}[/]")
            else:
                error = " ".join((m.error or "").split())[:60]
                log.write(f"  [red]✗[/] {m.probe_type.value} {m.target} [dim]{error}[/]")
        self.action_reload()  # the board now includes the new target

    def on_service_saved(self, message: ServiceSaved) -> None:
        log = self.query_one("#check_log", RichLog)
        if message.created:
            log.write(f"[cyan]★[/] saved service [bold]{message.name}[/] [dim]— pinned on the board even before its first check[/]")
        else:
            log.write(f"[cyan]★[/] updated service [bold]{message.name}[/]")
        self.action_reload()

    def on_service_save_failed(self, message: ServiceSaveFailed) -> None:
        self.query_one("#check_log", RichLog).write(f"[red]{message.detail}[/]")

    # ------------------------------------------------------------------
    # Rendering helpers (UI thread only)
    # ------------------------------------------------------------------

    def _fill_table(self, rows: list[RawMeasurement], services: list[Service]) -> None:
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

        log = self.query_one("#check_log", RichLog)

        if not last_seen and not services:
            log.write("[yellow]nothing to show yet[/] [dim]— run netscope diagnose, check a host above, or save a service ('name host')[/]")
            return

        # Saved services first (pinned): a curated entry is on the board
        # before its first check -- that is what the saved list is for.
        # The row is keyed by the service's host (what gets measured),
        # with the curated name as the label.
        covered_hosts: set[str] = set()
        for svc in services:
            covered_hosts.add(svc.host)
            seen = last_seen.get(svc.host)
            seen_text = f"[dim]{seen.timestamp.strftime('%m-%d %H:%M')}[/]" if seen is not None else "[dim]never[/]"
            cells = [self._status_cell(latest.get((svc.host, pt))) for pt in _CHECK_COLUMNS]
            table.add_row(f"[cyan]★ {svc.name}[/] [dim]{svc.host}[/]", *cells, seen_text)

        for target in sorted(last_seen):
            if target in covered_hosts:
                continue  # the pinned row above already shows this host
            cells = [self._status_cell(latest.get((target, pt))) for pt in _CHECK_COLUMNS]
            seen_stamp = last_seen[target].timestamp.strftime("%m-%d %H:%M")
            table.add_row(target, *cells, f"[dim]{seen_stamp}[/]")

        if services:
            log.write("[dim]★ = saved service — add/remove with netscope service add / netscope service remove[/]")

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
