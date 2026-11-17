"""
netscope.ui.tui.screens.network

The network information screen (TASK-042): what NetScope knows about
the local network at a glance -- every interface psutil reports (name,
classification, state, addresses) plus the gateway the shell resolved
for its rounds. Since v0.16 it also answers the question the CLI
answered in v0.12/v0.14 and the TUI never did: WHERE AM I and WHOSE
ROAD AM I ON -- the user's own line looked up in the same GeoLite2
databases every other surface reads, rendered with the same shared
summaries the evidence reports use, degrading honestly line by line
when no home is configured, no database is wired, or a database does
not know the address.

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

from netscope.app.reports import asn_summary, geo_summary
from netscope.core.asn import asn_extra
from netscope.core.geo import geo_extra
from netscope.core.models import NetworkSnapshot

if TYPE_CHECKING:
    from netscope.core.asn import AsnInfo
    from netscope.core.geo import GeoInfo
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


class HomeDone(Message):
    """One home-lookup pass (v0.16), posted from the worker thread to
    the UI thread. `lines` is the ready-to-render "your line" block --
    composed in the worker (the mmdb reads can block), applied by
    `_paint_card` on the UI thread."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self.lines = lines


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

    def __init__(self) -> None:
        super().__init__()
        self._latest_snapshot: NetworkSnapshot | None = None
        self._home_block: list[str] = []

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
    # Discovery + home lookups (background workers -> messages)
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._run_discover_worker()
        self._run_home_worker()

    @work(thread=True, exclusive=True, group="discover")
    def _run_discover_worker(self) -> None:
        try:
            snapshot = cast("NetScopeTUI", self.app).container.discovery_provider.discover()
        except Exception as exc:  # noqa: BLE001 -- discovery is best-effort decoration
            self.post_message(DiscoverDone(error=str(exc)))
            return
        self.post_message(DiscoverDone(snapshot=snapshot))

    @work(thread=True, exclusive=True, group="home")
    def _run_home_worker(self) -> None:
        self.post_message(HomeDone(lines=self._home_lines()))

    def on_discover_done(self, message: DiscoverDone) -> None:
        if message.snapshot is None:
            self._latest_snapshot = None
            self.query_one("#context_card", Static).update(
                "[bold]Network context[/]\n[red]unavailable[/]\n[dim]discovery failed:[/]\n[dim] "
                + " ".join((message.error or "").split())[:120]
                + "[/]"
            )
            return
        self._latest_snapshot = message.snapshot
        self._fill_table(message.snapshot)
        self._paint_card()

    def on_home_done(self, message: HomeDone) -> None:
        self._home_block = message.lines
        self._paint_card()

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

    def _paint_card(self) -> None:
        """Repaint the context card from the two facts it is made of --
        the discovery snapshot and the home block -- whichever landed
        last. A failed discovery keeps its own failure card until a
        re-discover answers; home lines arriving meanwhile are stored,
        not painted over it."""
        if self._latest_snapshot is None:
            return
        self.query_one("#context_card", Static).update(self._context_text(self._latest_snapshot) + self._home_section())

    def _home_section(self) -> str:
        if not self._home_block:
            return ""
        return "\n\n" + "\n".join(self._home_block)

    def _context_text(self, snapshot: NetworkSnapshot) -> str:
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
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # The "your line" block (v0.16) -- composed in the worker thread
    # ------------------------------------------------------------------

    def _home_lines(self) -> list[str]:
        """The user's own line, answered from the SAME sources every
        other surface reads: config's `home_ip` looked up in the wired
        GeoLite2-City and GeoLite2-ASN databases, rendered with the
        shared `geo_summary`/`asn_summary` wording -- database-age
        disclosures included, the EULA's aging notice travels with the
        fact. Every absence degrades to one honest dim line: no home
        configured, no database wired, an address the database does
        not know, a database that fails to answer -- never a crash,
        never a fabricated place."""
        shell = cast("NetScopeTUI", self.app)
        config = shell.config
        container = shell.container
        if not config.home_ip:
            if config.home_latitude is not None and config.home_longitude is not None:
                return [
                    "[bold]Your line[/]",
                    "[dim]home coordinates set — distance-from-home is active[/]",
                    "[dim]set home_ip in config.toml to name your own place and network[/]",
                ]
            return ["[bold]Your line[/]", "[dim]home unknown — set home_ip (or home coordinates) in config.toml[/]"]
        lines = [f"[bold]Your line[/] [dim]{config.home_ip}[/]"]
        geo = container.geo_lookup
        if geo is None:
            lines.append("[dim]no GeoLite2-City database wired — your place stays unnamed (docs/usage/geoip.md)[/]")
        else:
            info: GeoInfo | None = None
            failed = False
            try:
                info = geo.lookup(config.home_ip)
            except (ValueError, RuntimeError):
                failed = True
            if failed:
                lines.append("[red]the geo database exists but could not answer[/]")
            elif info is None:
                lines.append("[dim]the geo database does not know this address[/]")
            else:
                text = geo_summary(geo_extra(info))
                lines.append(text if text else "[dim]the geo database knows the address but names nothing[/]")
        asn = container.asn_lookup
        if asn is None:
            lines.append("[dim]no ASN source wired — your network stays unnamed (docs/usage/geoip.md)[/]")
        else:
            record: AsnInfo | None = None
            asn_failed = False
            try:
                record = asn.lookup(config.home_ip)
            except (ValueError, RuntimeError):
                asn_failed = True
            if asn_failed:
                lines.append("[red]the ASN source exists but could not answer[/]")
            elif record is None or record.asn is None:
                lines.append("[dim]the ASN source does not know this address[/]")
            else:
                lines.append(asn_summary(asn_extra(record)))
        return lines
