"""
netscope.ui.tui.screens.history

The history screen (TASK-045): recent measurement rounds reconstructed
from the local log, one row per round with its recomputed experience
score -- and the per-measurement detail of whichever round is selected.

WHAT IT SHOWS
-------------
`recent_rounds` (the use case behind `netscope history`) regroups the
flat measurement log into rounds and re-scores each against the
CURRENT personal baseline. That recomputation is an honest
approximation and the screen says so in its detail panel: the log
cannot reconstruct the baseline as it stood at each round's time (the
same caveat `netscope history` and `netscope report` disclose -- one
round definition, one scoring rule, three views).

Selecting a row (arrow keys + enter, or clicking) shows that round's
measurements verbatim: probe, target, latency or error. `r` reloads
from the log; with no measurement repository wired the screen says so
instead of pretending to poll something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from netscope.app.use_cases import HistoryRound, recent_rounds
from netscope.ui.tui.screens import LEVEL_COLORS

if TYPE_CHECKING:
    from netscope.ui.tui.app import NetScopeTUI


class HistoryLoaded(Message):
    """One log read, posted from the worker thread to the UI thread."""

    def __init__(self, rounds: list[HistoryRound]) -> None:
        super().__init__()
        self.rounds = rounds


class HistoryScreen(Screen):
    """Recent rounds from the measurement log, with per-round detail."""

    BINDINGS = [
        Binding("r", "reload", "Reload from log"),
    ]

    CSS = """
    #rounds_table {
        height: 1fr;
        border: round $panel;
    }
    #round_detail {
        height: 12;
        border: round $panel;
    }
    #history_status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._rounds: list[HistoryRound] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield DataTable(id="rounds_table")
            yield RichLog(id="round_detail", markup=True, wrap=True, highlight=False)
            yield Static("loading log…", id="history_status", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#rounds_table", DataTable)
        table.cursor_type = "row"  # a round is the selectable unit, not a cell
        table.add_columns("When", "Checks", "Score", "Level", "Failures")
        self.action_reload()

    # ------------------------------------------------------------------
    # Log reading (background worker -> HistoryLoaded message)
    # ------------------------------------------------------------------

    def action_reload(self) -> None:
        self.query_one("#history_status", Static).update("[dim]loading log…[/dim]")
        self._run_history_worker()

    @work(thread=True, exclusive=True, group="history")
    def _run_history_worker(self) -> None:
        shell = cast("NetScopeTUI", self.app)
        rounds = recent_rounds(shell.container, limit=25)
        self.post_message(HistoryLoaded(rounds=rounds))

    def on_history_loaded(self, message: HistoryLoaded) -> None:
        self._rounds = message.rounds
        self._fill_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Show the selected round's measurements verbatim."""
        log = self.query_one("#round_detail", RichLog)
        log.clear()
        if event.row_key.value is None:
            return
        try:
            index = int(event.row_key.value)
            round_ = self._rounds[index]
        except (ValueError, IndexError):
            return
        self._write_round_detail(log, round_)

    # ------------------------------------------------------------------
    # Rendering helpers (UI thread only)
    # ------------------------------------------------------------------

    def _fill_table(self) -> None:
        table = self.query_one("#rounds_table", DataTable)
        status = self.query_one("#history_status", Static)
        table.clear()
        log = self.query_one("#round_detail", RichLog)
        log.clear()

        if not self._rounds:
            status.update("[yellow]no rounds recorded yet[/] [dim]— run netscope diagnose (or wait for a round) with persistence wired; r reloads[/]")
            return

        for index, round_ in enumerate(self._rounds):
            failures = sum(1 for m in round_.measurements if not m.success)
            score_cell = "—"
            level_cell = "—"
            if round_.experience is not None:
                color = LEVEL_COLORS[round_.experience.level]
                score_cell = f"[bold {color}]{round_.experience.score:.0f}[/]"
                level_cell = f"[{color}]{round_.experience.level.value}[/]"
            table.add_row(
                round_.timestamp.strftime("%m-%d %H:%M:%S"),
                str(len(round_.measurements)),
                score_cell,
                level_cell,
                str(failures) if failures else "[green]0[/]",
                key=str(index),
            )
        status.update(
            f"{len(self._rounds)} round(s), oldest first · scores recomputed against the current baseline · [dim]enter: round detail · r: reload[/]"
        )

    def _write_round_detail(self, log: RichLog, round_: HistoryRound) -> None:
        log.write(f"[bold]round at {round_.timestamp.strftime('%Y-%m-%d %H:%M:%S')}[/]")
        if round_.experience is not None:
            log.write(
                f"[dim]score {round_.experience.score:.0f}/100 ({round_.experience.level.value})"
                f" — recomputed against the current baseline, not the historical one[/]"
            )
        for m in round_.measurements:
            if m.success:
                latency = f"{m.latency_ms:.1f} ms" if m.latency_ms is not None else ""
                log.write(f"  [green]✓[/] {m.probe_type.value} {m.target} [dim]{latency}[/]")
            else:
                error = " ".join((m.error or "").split())[:60]
                log.write(f"  [red]✗[/] {m.probe_type.value} {m.target} [dim]{error}[/]")
