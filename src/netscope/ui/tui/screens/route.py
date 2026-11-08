"""
netscope.ui.tui.screens.route

The route screen (TASK-043): traceroute with the same enrichment the
CLI's `route` command shows -- reverse-DNS hostnames per hop (when a
resolver is wired), AS numbers per hop (when the offline ASN database
exists), and the churn verdict over the snapshots NetScope has for the
target.

WHY IT WORKS THIS WAY
---------------------
The route pipeline is NOT re-implemented here: the screen calls
`run_route_analysis`, the exact use case behind `netscope route` --
one traceroute implementation, two views (the docstring on that use
case explains why it lives in `app`, not `ui`). A route view that
computed its own hops would drift from the CLI's the first time
enrichment changed.

Tracing runs on a background worker (traceroute is the slowest probe
NetScope has -- seconds, not milliseconds), so the event loop keeps
breathing while hops accumulate. `Enter` traces whatever is in the
input, `r` re-traces the last target. A failed traceroute renders as a
failed traceroute -- never as an empty route (the same honesty rule
the CLI follows).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

from netscope.app.use_cases import RouteAnalysisResult, run_route_analysis

if TYPE_CHECKING:
    from netscope.ui.tui.app import NetScopeTUI


class RouteDone(Message):
    """One route analysis, posted from the worker thread to the UI
    thread."""

    def __init__(self, target: str, result: RouteAnalysisResult) -> None:
        super().__init__()
        self.target = target
        self.result = result


class RouteScreen(Screen):
    """Traceroute + enrichment + churn, on demand."""

    BINDINGS = [
        Binding("r", "retrace", "Re-trace"),
    ]

    # No initial focus steal: screen keys (r, d, …) must work the moment
    # the screen opens; tab focuses the target input when needed.
    AUTO_FOCUS = None

    CSS = """
    #target_input {
        margin: 0 1;
    }
    #hops_table {
        height: 1fr;
        border: round $panel;
    }
    #route_status {
        height: 3;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_target: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Input(placeholder="host or IP to trace…", id="target_input")
            yield DataTable(id="hops_table")
            yield Static("", id="route_status", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#hops_table", DataTable)
        table.add_columns("TTL", "Host", "Address", "ASN", "Loss", "Avg RTT")
        shell = cast("NetScopeTUI", self.app)
        default = shell.config.public_dns_target
        target_input = self.query_one("#target_input", Input)
        target_input.value = default
        target_input.blur()  # screen keys first; tab focuses the input
        self._trace(default)

    # ------------------------------------------------------------------
    # Tracing (background worker -> RouteDone message)
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "target_input":
            return
        target = event.value.strip()
        if not target:
            return
        event.input.blur()  # hand keys back to the screen (r, d, …)
        self._trace(target)

    def action_retrace(self) -> None:
        if self._last_target is None:
            return
        self._trace(self._last_target)

    def _trace(self, target: str) -> None:
        self._last_target = target
        self.query_one("#route_status", Static).update(f"[dim]tracing {target}…[/dim]")
        self._run_route_worker(target)

    @work(thread=True, exclusive=True, group="route")
    def _run_route_worker(self, target: str) -> None:
        shell = cast("NetScopeTUI", self.app)
        result = run_route_analysis(target, shell.container, resolve_hostnames=True)
        self.post_message(RouteDone(target=target, result=result))

    def on_route_done(self, message: RouteDone) -> None:
        self._fill_table(message.target, message.result)

    # ------------------------------------------------------------------
    # Rendering helpers (UI thread only)
    # ------------------------------------------------------------------

    def _fill_table(self, target: str, result: RouteAnalysisResult) -> None:
        table = self.query_one("#hops_table", DataTable)
        table.clear()
        status = self.query_one("#route_status", Static)

        if not result.success:
            error = " ".join((result.measurement.error or "traceroute failed").split())[:80]
            status.update(f"[red]✗ route to {target} failed[/] [dim]— {error}[/]")
            return

        for hop in result.snapshot.hops if result.snapshot is not None else []:
            host = hop.hostname or "—"
            address = hop.address or "—"
            ttl_cell = f"[yellow]{hop.ttl}![/]" if hop.is_unstable else str(hop.ttl)
            table.add_row(
                ttl_cell,
                host,
                address,
                hop.asn or "—",
                f"{hop.packet_loss_pct:.0f}%",
                f"{hop.avg_rtt_ms:.1f} ms" if hop.avg_rtt_ms is not None else "—",
            )

        churn = result.churn
        churn_line = ""
        if churn is not None:
            if churn.change_count == 0:
                churn_line = " · stable"
            else:
                churn_line = f" · {churn.change_count} change(s) across {churn.snapshot_count} snapshot(s)"
        status.update(
            f"[green]✓[/] route to [bold]{target}[/] · {table.row_count} hops{churn_line}"
            f" · [dim]enter: trace a target · r: re-trace[/]"
        )
