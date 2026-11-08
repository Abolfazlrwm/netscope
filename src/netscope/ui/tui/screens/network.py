"""
netscope.ui.tui.screens.network

The network information screen (TASK-042): what NetScope knows about
the local network at a glance -- every interface psutil reports (name,
classification, state, addresses) plus the gateway the shell resolved
for its rounds.

WHY A SCREEN FOR THIS
---------------------
The dashboard's network panel is deliberately decoration -- one bounded
card, best-effort, squeezed next to the diagnosis. Phase I reserved a
full screen for network context (TASK-042) because "which interface am
I on, and what kind of network is it" is the first question most
diagnosis sessions actually start from (a Wi-Fi/cellular answer
reframes every downstream verdict).

Data comes from the same `DiscoveryProvider` the dashboard panel uses
-- one discovery implementation, two views. Discovery runs on a
background worker (`r` re-discovers), since psutil enumeration can
block on busy machines; the screen never blocks the event loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from netscope.core.models import NetworkSnapshot

if TYPE_CHECKING:
    from netscope.ui.tui.app import NetScopeTUI


class DiscoverDone(Message):
    """One discovery attempt, posted from the worker thread to the UI
    thread. Exactly one of snapshot/error is set -- a failed discovery
    is rendered as unavailable, never as an empty network."""

    snapshot: NetworkSnapshot | None
    error: str | None

    def __init__(self, snapshot: NetworkSnapshot | None = None, error: str | None = None) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.error = error


class NetworkScreen(Screen):
    """The local network at a glance: interfaces, types, gateway."""

    BINDINGS = [
        Binding("r", "refresh", "Re-discover"),
    ]

    CSS = """
    #ifaces_table {
        width: 1fr;
        height: 1fr;
        border: round $panel;
    }
    #context_card {
        width: 42;
        height: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield DataTable(id="ifaces_table")
            with Vertical():
                yield Static("discovering…", id="context_card", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ifaces_table", DataTable)
        table.add_columns("Interface", "Type", "State", "Addresses")
        self.action_refresh()

    # ------------------------------------------------------------------
    # Discovery (background worker -> DiscoverDone message)
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._run_discover_worker()

    @work(thread=True, exclusive=True, group="discover")
    def _run_discover_worker(self) -> None:
        try:
            snapshot = cast("NetScopeTUI", self.app).container.discovery_provider.discover()
        except Exception as exc:  # noqa: BLE001 -- discovery is best-effort decoration
            self.post_message(DiscoverDone(error=str(exc)))
            return
        self.post_message(DiscoverDone(snapshot=snapshot))

    def on_discover_done(self, message: DiscoverDone) -> None:
        if message.snapshot is None:
            self.query_one("#context_card", Static).update(
                "[bold]Network context[/]\n[red]unavailable[/]\n[dim]discovery failed:[/]\n[dim] "
                + " ".join((message.error or "").split())[:120]
                + "[/]"
            )
            return
        self._fill_table(message.snapshot)
        self._update_context_card(message.snapshot)

    # ------------------------------------------------------------------
    # Rendering helpers (UI thread only)
    # ------------------------------------------------------------------

    def _fill_table(self, snapshot: NetworkSnapshot) -> None:
        table = self.query_one("#ifaces_table", DataTable)
        table.clear()
        for iface in snapshot.interfaces:
            state = "[green]● up[/]" if iface.is_up else "[dim]○ down[/]"
            addresses = "\n".join(iface.addresses[:2]) or "—"
            table.add_row(iface.name, iface.network_type.value, state, addresses)

    def _update_context_card(self, snapshot: NetworkSnapshot) -> None:
        shell = cast("NetScopeTUI", self.app)
        total = len(snapshot.interfaces)
        up = sum(1 for iface in snapshot.interfaces if iface.is_up)
        primary = next((iface for iface in snapshot.interfaces if iface.is_up and not iface.is_loopback), None)
        lines = [
            "[bold]Network context[/]",
            f"gateway: [bold]{shell.gateway or 'not detected'}[/]",
            f"interfaces: {up}/{total} up",
        ]
        if primary is not None:
            lines.append(f"primary: [bold]{primary.name}[/] [dim]({primary.network_type.value})[/]")
        lines.append(f"[dim]discovered {snapshot.timestamp.strftime('%H:%M:%S')} · r re-discover[/]")
        self.query_one("#context_card", Static).update("\n".join(lines))
