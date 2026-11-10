"""
TUI smoke + behavior tests (tests/test_tui.py).

The TUI is tested fully offline: a fake round_fn returns scripted
measurements/experience/diagnosis, so no real probe ever runs and no
wall-clock interval matters (interval is set to an hour; the test
calls request_round() directly and waits for the RoundDone message to
land). Since v0.6 the same offline pattern drives every SCREEN:
navigation keys, the network/route/history/services views (with fake
discovery, fake traceroute, and an in-memory log), and the shell's
replay-the-latest-round delivery. Requires textual (installed with the
dev extra) -- skipped cleanly otherwise.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.app.gateway import resolve_gateway
from netscope.core.models import (
    SERVICE_CHECK_TYPES,
    Diagnosis,
    Evidence,
    ExperienceEvent,
    ExperienceLevel,
    Hypothesis,
    NetworkInterface,
    NetworkSnapshot,
    ProbeType,
    RawMeasurement,
    RouteHop,
    RouteSnapshot,
    Service,
    Severity,
    utcnow,
)


class _FakeProbe:
    """Never actually called (round_fn is faked); satisfies the registry."""

    def __init__(self, probe_type: ProbeType):
        self.probe_type = probe_type
        self.calls: list[str] = []

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        return RawMeasurement(probe_type=self.probe_type, target=target, success=True, latency_ms=1.0)


def _fake_container(
    discovery: _FakeDiscovery | None = None,
    repository: _FakeRepo | None = None,
    service_repository: ServiceRepository | None = None,
) -> Container:
    probes = {p: _FakeProbe(p) for p in (ProbeType.ICMP, ProbeType.DNS, ProbeType.HTTP, ProbeType.TCP, ProbeType.TLS, ProbeType.TRACEROUTE)}
    kwargs: dict = {}
    if discovery is not None:
        kwargs["discovery_provider"] = discovery
    if repository is not None:
        kwargs["measurement_repository"] = repository
    if service_repository is not None:
        kwargs["service_repository"] = service_repository
    return Container(probe_registry=ProbeRegistry(probes=probes), **kwargs)


class _ScriptedTraceroute:
    """Traceroute adapter double: returns a canned RouteSnapshot (or a
    canned failure) without touching the network."""

    probe_type = ProbeType.TRACEROUTE

    def __init__(self, hops: list[RouteHop] | None = None, error: str | None = None):
        self.hops = hops
        self.error = error

    def run(self, target: str, **options) -> RawMeasurement:
        if self.error is not None:
            return RawMeasurement(probe_type=ProbeType.TRACEROUTE, target=target, success=False, error=self.error)
        assert self.hops is not None
        return RawMeasurement(
            probe_type=ProbeType.TRACEROUTE,
            target=target,
            success=True,
            latency_ms=None,
            extra={"route": RouteSnapshot(target=target, hops=list(self.hops))},
        )


class _FakeDiscovery:
    """Discovery adapter double: returns one canned NetworkSnapshot."""

    def __init__(self, snapshot: NetworkSnapshot):
        self.snapshot = snapshot

    def discover(self) -> NetworkSnapshot:
        return self.snapshot


class _FakeRepo:
    """In-memory stand-in for MeasurementRepository: latest() returns
    rows newest-first, save() records them -- enough for the history
    and services screens, fully offline."""

    def __init__(self, rows: list[RawMeasurement] | None = None):
        self.rows: list[RawMeasurement] = list(rows or [])

    def latest(self, limit: int = 200) -> list[RawMeasurement]:
        return self.rows[:limit]

    def save(self, measurement: RawMeasurement) -> None:
        self.rows.insert(0, measurement)


textual = pytest.importorskip("textual")

from netscope.persistence.service_repository import ServiceRepository  # noqa: E402
from netscope.ui.tui.app import NetScopeTUI  # noqa: E402 -- after importorskip
from netscope.ui.tui.screens.dashboard import DashboardScreen  # noqa: E402
from netscope.ui.tui.screens.diagnostics import DiagnosticsScreen  # noqa: E402
from netscope.ui.tui.screens.history import HistoryScreen  # noqa: E402
from netscope.ui.tui.screens.network import NetworkScreen  # noqa: E402
from netscope.ui.tui.screens.route import RouteScreen  # noqa: E402
from netscope.ui.tui.screens.services import ServicesScreen  # noqa: E402


def _ok_measurement(probe: ProbeType, target: str, latency: float) -> RawMeasurement:
    return RawMeasurement(probe_type=probe, target=target, success=True, latency_ms=latency)


def _experience(score: float = 88.0, level: ExperienceLevel = ExperienceLevel.GOOD) -> ExperienceEvent:
    return ExperienceEvent(timestamp=utcnow(), score=score, level=level)


def _fake_round_fn(rounds: list[tuple]):
    """Build a round_fn that replays `rounds` (one tuple per call,
    repeating the last one forever)."""
    calls = {"n": 0}

    def round_fn(container, config, gateway):
        index = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        return rounds[index]

    return round_fn


def _healthy_round(score: float = 88.0):
    measurements = [
        _ok_measurement(ProbeType.ICMP, "192.168.1.1", 2.0),
        _ok_measurement(ProbeType.ICMP, "1.1.1.1", 9.0),
        _ok_measurement(ProbeType.DNS, "example.com", 12.0),
        _ok_measurement(ProbeType.HTTP, "https://www.cloudflare.com/", 30.0),
    ]
    return measurements, _experience(score), None


def _failing_round():
    measurements = [
        RawMeasurement(probe_type=ProbeType.ICMP, target="1.1.1.1", success=False, error="timeout"),
        _ok_measurement(ProbeType.DNS, "example.com", 12.0),
        RawMeasurement(probe_type=ProbeType.HTTP, target="https://www.cloudflare.com/", success=False, error="connection refused"),
    ]
    diagnosis = Diagnosis(
        classification=Hypothesis.DESTINATION_ISSUE,
        evidence=[Evidence(metric="destination_latency", tested=True, severity=Severity.CRITICAL)],
        confidence=0.9,
    )
    return measurements, _experience(20.0, ExperienceLevel.POOR), diagnosis


def _make_app(rounds: list[tuple]) -> NetScopeTUI:
    container = _fake_container()  # fake probes + no repositories: fully offline
    config = NetScopeConfig()
    return NetScopeTUI(container=container, config=config, interval=3600.0, round_fn=_fake_round_fn(rounds))


async def _wait_for_rounds(pilot, app: NetScopeTUI, count: int, timeout: float = 5.0) -> bool:
    """Pump the message loop until `count` rounds have landed (or
    timeout) -- the round runs on a worker thread, so determinism comes
    from polling app state with pauses, never sleeps."""
    waited = 0.0
    while app._round_count < count and waited < timeout:
        await pilot.pause(0.05)
        waited += 0.05
    await pilot.pause()
    return app._round_count >= count


async def test_tui_boots_and_renders_first_round():
    app = _make_app([_healthy_round()])
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait_for_rounds(pilot, app, 1)
        score_card = str(app.query_one("#score_card").render())
        assert "88" in score_card
        assert "GOOD" in score_card.upper()
        checks = app.query_one("#checks_table")
        assert checks.row_count == 4


async def test_tui_updates_score_history_across_rounds():
    app = _make_app([_healthy_round(88.0), _healthy_round(72.0)])
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait_for_rounds(pilot, app, 1)
        app.request_round()  # the interval is an hour: trigger round 2 explicitly
        assert await _wait_for_rounds(pilot, app, 2)
        assert app._round_count == 2
        assert app._score_history[-1] == 72.0
        assert len(app._score_history) == 2


def _log_text(app: NetScopeTUI, selector: str) -> str:
    """The full written text of a RichLog panel (render() returns the
    panel object, not the lines)."""
    from textual.widgets import RichLog

    log = app.query_one(selector, RichLog)
    return "\n".join(strip.text for strip in log.lines)


async def test_tui_shows_diagnosis_for_a_failing_round():
    app = _make_app([_failing_round()])
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait_for_rounds(pilot, app, 1)
        diagnosis_text = _log_text(app, "#diagnosis_card")
        assert "Destination-specific issue" in diagnosis_text
        assert "destination_latency" in diagnosis_text


async def test_tui_survives_a_crashing_round_fn():
    def exploding_round_fn(container, config, gateway):
        raise RuntimeError("boom")

    app = NetScopeTUI(container=_fake_container(), config=NetScopeConfig(), interval=3600.0, round_fn=exploding_round_fn)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for_rounds(pilot, app, 0)
        log_text = _log_text(app, "#log")
        assert "boom" in log_text or "round failed" in log_text
        assert app._round_count == 0  # no fabricated round


def test_resolve_gateway_none_sentinel_disables_detection():
    """'none' must short-circuit even when config/detector would answer."""
    config = NetScopeConfig(gateway="10.9.9.9")
    assert resolve_gateway("none", config) is None
    assert resolve_gateway("192.168.0.254", config) == "192.168.0.254"
    assert resolve_gateway(None, config) == "10.9.9.9"


# ---------------------------------------------------------------------------
# v0.6: the six screens
# ---------------------------------------------------------------------------


async def _wait_active(pilot, app: NetScopeTUI, screen_type: type) -> None:
    """Wait until `screen_type` is the active screen (screen pushes
    resolve across message-loop cycles)."""
    waited = 0.0
    while not isinstance(app.screen, screen_type) and waited < 5.0:
        await pilot.pause(0.05)
        waited += 0.05
    await pilot.pause()
    assert isinstance(app.screen, screen_type), f"expected {screen_type.__name__}, got {type(app.screen).__name__}"


async def _wait_for(predicate, pilot, timeout: float = 5.0) -> None:
    """Pump the message loop until `predicate()` holds (screen workers
    post their results across cycles; never sleep for real)."""
    waited = 0.0
    while not predicate() and waited < timeout:
        await pilot.pause(0.05)
        waited += 0.05
    await pilot.pause()
    assert predicate(), "condition never became true"


async def test_screen_keys_visit_all_six_screens():
    app = _make_app([_healthy_round()])
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait_for_rounds(pilot, app, 1)
        for key, screen_type in [
            ("n", NetworkScreen),
            ("p", RouteScreen),
            ("e", DiagnosticsScreen),
            ("h", HistoryScreen),
            ("s", ServicesScreen),
            ("d", DashboardScreen),
        ]:
            await pilot.press(key)
            await _wait_active(pilot, app, screen_type)


async def test_network_screen_lists_interfaces_and_gateway():
    snapshot = NetworkSnapshot(
        timestamp=utcnow(),
        interfaces=[
            NetworkInterface(name="lo", is_up=True, addresses=["127.0.0.1"], is_loopback=True),
            NetworkInterface(name="eth0", is_up=True, addresses=["192.168.1.10", "fd00::5"]),
        ],
    )
    app = NetScopeTUI(
        container=_fake_container(discovery=_FakeDiscovery(snapshot)),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("n")
        await _wait_active(pilot, app, NetworkScreen)
        screen = app.screen
        table = screen.query_one("#ifaces_table")
        await _wait_for(lambda: table.row_count == 2, pilot)
        card = str(screen.query_one("#context_card").render())
        assert "gateway:" in card
        assert "eth0" in card


def _route_app(hops: list[RouteHop] | None, error: str | None, *, route_repository=None) -> NetScopeTUI:
    probes = {p: _FakeProbe(p) for p in (ProbeType.ICMP, ProbeType.DNS, ProbeType.HTTP, ProbeType.TCP, ProbeType.TLS)}
    probes[ProbeType.TRACEROUTE] = _ScriptedTraceroute(hops=hops, error=error)
    container = Container(probe_registry=ProbeRegistry(probes=probes))
    if route_repository is not None:
        container.route_repository = route_repository
    return NetScopeTUI(
        container=container,
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )


async def test_route_screen_traces_and_renders_hops():
    hops = [
        RouteHop(ttl=1, address="192.168.1.1", hostname="fritz.box", avg_rtt_ms=2.1, packet_loss_pct=0.0),
        RouteHop(ttl=2, address="81.19.104.1", hostname=None, avg_rtt_ms=9.8, packet_loss_pct=0.0, asn="AS6830"),
    ]
    app = _route_app(hops=hops, error=None)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("p")
        await _wait_active(pilot, app, RouteScreen)
        screen = app.screen
        await _wait_for(lambda: screen.query_one("#hops_table").row_count == 2, pilot)
        status = str(screen.query_one("#route_status").render())
        assert "1.1.1.1" in status  # the default target (config.public_dns_target)
        # v0.8 route memory: this test's container wires no RouteRepository,
        # so the honest verdict for a single in-memory snapshot is a first
        # observation -- "stable" would claim a certainty the evidence
        # does not support.
        assert "first observation" in status


async def test_route_screen_reports_stability_across_a_stored_history():
    """With route memory wired, the status tail carries the cross-run
    verdict -- compact density, run count included."""
    from datetime import UTC, datetime

    class _RememberingRepo:
        def save(self, snapshot: RouteSnapshot) -> None: ...

        def load_recent(self, target: str, limit: int = 20) -> list[RouteSnapshot]:
            return [
                RouteSnapshot(
                    target=target,
                    timestamp=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
                    hops=[
                        RouteHop(ttl=1, address="192.168.1.1", hostname="fritz.box", avg_rtt_ms=2.1, packet_loss_pct=0.0),
                        RouteHop(ttl=2, address="81.19.104.1", hostname=None, avg_rtt_ms=9.8, packet_loss_pct=0.0),
                    ],
                )
            ]

    hops = [
        RouteHop(ttl=1, address="192.168.1.1", hostname="fritz.box", avg_rtt_ms=2.1, packet_loss_pct=0.0),
        RouteHop(ttl=2, address="81.19.104.1", hostname=None, avg_rtt_ms=9.8, packet_loss_pct=0.0),
    ]
    app = _route_app(hops=hops, error=None, route_repository=_RememberingRepo())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("p")
        await _wait_active(pilot, app, RouteScreen)
        screen = app.screen
        await _wait_for(lambda: screen.query_one("#hops_table").row_count == 2, pilot)
        status = str(screen.query_one("#route_status").render())
        assert "stable across 2 runs" in status


async def test_route_screen_reports_a_failed_traceroute_honestly():
    app = _route_app(hops=None, error="probe timed out")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("p")
        await _wait_active(pilot, app, RouteScreen)
        screen = app.screen
        await _wait_for(lambda: "failed" in str(screen.query_one("#route_status").render()), pilot)
        assert screen.query_one("#hops_table").row_count == 0  # never an empty fabricated route


async def test_diagnostics_screen_replays_the_latest_round_in_full():
    app = _make_app([_failing_round()])
    async with app.run_test(size=(130, 40)) as pilot:
        assert await _wait_for_rounds(pilot, app, 1)
        await pilot.press("e")
        await _wait_active(pilot, app, DiagnosticsScreen)
        screen = app.screen
        headline = str(screen.query_one("#diag_headline").render())
        assert "20" in headline and "POOR" in headline
        assert screen.query_one("#evidence_table").row_count >= 1
        from textual.widgets import RichLog

        explanation = "\n".join(strip.text for strip in screen.query_one("#explanation_log", RichLog).lines)
        assert "Likely cause" in explanation


async def test_history_screen_without_a_log_says_so():
    app = _make_app([_healthy_round()])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("h")
        await _wait_active(pilot, app, HistoryScreen)
        screen = app.screen
        await _wait_for(lambda: "no rounds recorded yet" in str(screen.query_one("#history_status").render()), pilot)


async def test_history_screen_lists_rounds_and_shows_detail():
    now = utcnow()
    rows: list[RawMeasurement] = []
    for offset in range(2):
        start = now - timedelta(minutes=6 - offset * 3)
        rows.append(RawMeasurement(probe_type=ProbeType.ICMP, target="1.1.1.1", timestamp=start, success=True, latency_ms=9.0))
        rows.append(
            RawMeasurement(
                probe_type=ProbeType.DNS,
                target="example.com",
                timestamp=start + timedelta(seconds=1),
                success=True,
                latency_ms=12.0,
            )
        )
    app = NetScopeTUI(
        container=_fake_container(repository=_FakeRepo(rows)),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("h")
        await _wait_active(pilot, app, HistoryScreen)
        screen = app.screen
        table = screen.query_one("#rounds_table")
        await _wait_for(lambda: table.row_count == 2, pilot)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)
        from textual.widgets import RichLog

        detail = "\n".join(strip.text for strip in screen.query_one("#round_detail", RichLog).lines)
        assert "1.1.1.1" in detail


async def test_services_screen_builds_the_status_board_from_the_log():
    now = utcnow()
    rows = [
        RawMeasurement(probe_type=ProbeType.ICMP, target="192.168.1.1", timestamp=now - timedelta(minutes=2), success=True, latency_ms=2.1),
        RawMeasurement(probe_type=ProbeType.DNS, target="example.com", timestamp=now - timedelta(minutes=1), success=False, error="timeout"),
        RawMeasurement(
            probe_type=ProbeType.HTTP, target="https://www.cloudflare.com/", timestamp=now - timedelta(minutes=1), success=True, latency_ms=30.5
        ),
        # A bandwidth row is throughput evidence, never a service-check cell.
        RawMeasurement(probe_type=ProbeType.BANDWIDTH, target="https://speed.cloudflare.com/", timestamp=now - timedelta(minutes=3), success=True),
    ]
    app = NetScopeTUI(
        container=_fake_container(repository=_FakeRepo(rows)),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("s")
        await _wait_active(pilot, app, ServicesScreen)
        screen = app.screen
        table = screen.query_one("#services_table")
        await _wait_for(lambda: table.row_count == 4, pilot)  # 4 targets, incl. the bandwidth-only one
        rows_by_target = {str(table.get_row_at(i)[0]): table.get_row_at(i) for i in range(table.row_count)}
        assert "✓" in str(rows_by_target["https://www.cloudflare.com/"][5])  # HTTP cell healthy
        assert "✗" in str(rows_by_target["example.com"][2])  # DNS cell failed
        bandwidth_cells = rows_by_target["https://speed.cloudflare.com/"]
        assert all(str(cell) == "[dim]—[/]" for cell in bandwidth_cells[1:6])  # no fabricated check cells


async def test_services_screen_checks_a_host_on_demand_and_persists():
    repo = _FakeRepo()
    app = NetScopeTUI(
        container=_fake_container(repository=repo),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("s")
        await _wait_active(pilot, app, ServicesScreen)
        screen = app.screen
        box = screen.query_one("#service_input")
        box.focus()
        await pilot.pause()
        for char in "example.com":
            await pilot.press(char)
        await pilot.press("enter")
        await _wait_for(lambda: len(repo.rows) == 5, pilot)  # the five checks, persisted like the CLI
        from textual.widgets import RichLog

        log_text = "\n".join(strip.text for strip in screen.query_one("#check_log", RichLog).lines)
        assert "example.com" in log_text and "score" in log_text
        table = screen.query_one("#services_table")
        await _wait_for(lambda: table.row_count == 1, pilot)  # the board grew to include the new target


# ---------------------------------------------------------------------------
# v0.7 -- saved services on the board (TASK-032's storage half)
# ---------------------------------------------------------------------------


async def test_services_screen_pins_a_saved_service_before_its_first_check(tmp_path):
    """The whole point of the curated list: a saved entry is on the
    board immediately -- dashes for never-measured cells, 'never' for
    last seen -- even with an empty log."""
    service_repo = ServiceRepository.open(tmp_path / "services.db")
    service_repo.save(Service(name="My VPN", host="vpn.example.net", enabled_checks={ProbeType.ICMP}))
    app = NetScopeTUI(
        container=_fake_container(service_repository=service_repo),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("s")
        await _wait_active(pilot, app, ServicesScreen)
        screen = app.screen
        table = screen.query_one("#services_table")
        await _wait_for(lambda: table.row_count == 1, pilot)
        row = table.get_row_at(0)
        assert "My VPN" in str(row[0]) and "vpn.example.net" in str(row[0])
        # DataTable cells keep the raw markup string (same convention
        # the v0.6 board tests assert against).
        assert all(str(cell) == "[dim]—[/]" for cell in row[1:6])  # absent evidence shown as absent
        assert str(row[6]) == "[dim]never[/]"


async def test_services_screen_joins_saved_and_logged_targets_without_duplicating(tmp_path):
    """A host that is both saved and logged shows exactly once -- the
    pinned row, with the host's real evidence -- never twice."""
    service_repo = ServiceRepository.open(tmp_path / "services.db")
    service_repo.save(Service(name="My VPN", host="vpn.example.net", enabled_checks={ProbeType.ICMP}))
    now = utcnow()
    rows = [
        RawMeasurement(probe_type=ProbeType.ICMP, target="vpn.example.net", timestamp=now, success=True, latency_ms=3.0),
        RawMeasurement(probe_type=ProbeType.ICMP, target="other.example.net", timestamp=now, success=True, latency_ms=4.0),
    ]
    app = NetScopeTUI(
        container=_fake_container(repository=_FakeRepo(rows), service_repository=service_repo),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("s")
        await _wait_active(pilot, app, ServicesScreen)
        screen = app.screen
        table = screen.query_one("#services_table")
        await _wait_for(lambda: table.row_count == 2, pilot)
        first = str(table.get_row_at(0)[0])
        assert "My VPN" in first  # saved services render first
        targets = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert sum("vpn.example.net" in target for target in targets) == 1
        assert "✓" in str(table.get_row_at(0)[1])  # the pinned row carries the host's evidence


async def test_services_screen_saves_a_service_from_the_input(tmp_path):
    """'name host' in the input saves through the same use case as
    `netscope service add` (all five checks) and pins the row."""
    service_repo = ServiceRepository.open(tmp_path / "services.db")
    app = NetScopeTUI(
        container=_fake_container(service_repository=service_repo),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("s")
        await _wait_active(pilot, app, ServicesScreen)
        screen = app.screen
        box = screen.query_one("#service_input")
        box.focus()
        await pilot.pause()
        box.value = "My VPN vpn.example.net"
        await pilot.press("enter")
        await _wait_for(lambda: len(service_repo.load_all()) == 1, pilot)
        saved = service_repo.load_all()[0]
        assert saved.name == "My VPN" and saved.host == "vpn.example.net"
        assert saved.enabled_checks == SERVICE_CHECK_TYPES  # all five, as the placeholder promises

        table = screen.query_one("#services_table")
        await _wait_for(lambda: table.row_count == 1, pilot)
        assert "My VPN" in str(table.get_row_at(0)[0])


async def test_services_screen_saves_a_multi_word_name_last_word_is_host(tmp_path):
    """Hosts never contain spaces, so the board's grammar is: the last
    word is the host, everything before it is the curated name -- 'My
    VPN vpn.example.net' saves as name 'My VPN'."""
    service_repo = ServiceRepository.open(tmp_path / "services.db")
    app = NetScopeTUI(
        container=_fake_container(service_repository=service_repo),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("s")
        await _wait_active(pilot, app, ServicesScreen)
        screen = app.screen
        box = screen.query_one("#service_input")
        box.focus()
        await pilot.pause()
        box.value = "Home Router 192.168.1.1"
        await pilot.press("enter")
        await _wait_for(lambda: len(service_repo.load_all()) == 1, pilot)
        saved = service_repo.load_all()[0]
        assert saved.name == "Home Router"
        assert saved.host == "192.168.1.1"


async def test_services_screen_save_failure_is_honest_when_no_repository_is_wired():
    """Saving without a ServiceRepository fails loudly on the board --
    never a silent drop (the use case's own RuntimeError, relayed)."""
    app = NetScopeTUI(
        container=_fake_container(),
        config=NetScopeConfig(),
        interval=3600.0,
        round_fn=_fake_round_fn([_healthy_round()]),
    )
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("s")
        await _wait_active(pilot, app, ServicesScreen)
        screen = app.screen
        box = screen.query_one("#service_input")
        box.focus()
        await pilot.pause()
        box.value = "My VPN vpn.example.net"
        await pilot.press("enter")
        await pilot.pause(0.3)
        from textual.widgets import RichLog

        log_text = "\n".join(strip.text for strip in screen.query_one("#check_log", RichLog).lines)
        assert "ServiceRepository" in log_text
