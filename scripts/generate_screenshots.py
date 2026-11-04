"""Generate SVG screenshots for the README:
1. CLI diagnose output (rich Console export_svg) with fake healthy data.
2. CLI route output with a plausible hop table.
3. TUI dashboard (Textual export_screenshot) driven with fake data.

Run:  python scripts/generate_screenshots.py
Outputs: docs/images/cli-diagnose.svg, cli-route.svg, tui-dashboard.svg
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rich.console import Console

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.app.use_cases import run_measurement_round
from netscope.core.models import ExperienceLevel, ProbeType, RawMeasurement, RouteHop
from netscope.ui.render import diagnosis_panel, measurements_table, route_table
from netscope.ui.tui.app import NetScopeTUI

IMAGES = Path(__file__).parent.parent / "docs" / "images"


class _FakeProbe:
    def __init__(self, probe_type: ProbeType, latency: float):
        self.probe_type = probe_type
        self._latency = latency
        self.calls: list[str] = []

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        return RawMeasurement(probe_type=self.probe_type, target=target, success=True, latency_ms=self._latency)


def fake_container() -> Container:
    probes = {
        ProbeType.ICMP: _FakeProbe(ProbeType.ICMP, 8.4),
        ProbeType.DNS: _FakeProbe(ProbeType.DNS, 21.7),
        ProbeType.HTTP: _FakeProbe(ProbeType.HTTP, 38.2),
        ProbeType.TCP: _FakeProbe(ProbeType.TCP, 12.1),
        ProbeType.TLS: _FakeProbe(ProbeType.TLS, 41.5),
        ProbeType.TRACEROUTE: _FakeProbe(ProbeType.TRACEROUTE, 0.0),
    }
    return Container(probe_registry=ProbeRegistry(probes=probes))


def cli_screenshot() -> None:
    container = fake_container()
    config = NetScopeConfig()
    measurements, experience, diagnosis = run_measurement_round(container, config, gateway="192.168.1.1")
    experience.score, experience.level = 91.0, ExperienceLevel.GOOD

    console = Console(record=True, width=92, force_terminal=False, color_system="truecolor")
    console.print()
    console.print("[bold]NetScope diagnose[/] [dim]· gateway 192.168.1.1 · baseline: 24 samples[/]")
    console.print(f"Experience score: {experience.score:.0f}/100 ({experience.level.value})")
    console.print(measurements_table(measurements))
    console.print()
    console.print(diagnosis_panel(diagnosis))
    svg = console.export_svg(title="netscope diagnose")
    (IMAGES / "cli-diagnose.svg").write_text(svg, encoding="utf-8")
    print("cli-diagnose.svg written")


def route_screenshot() -> None:
    hops = [
        RouteHop(ttl=1, address="192.168.1.1", hostname="fritz.box", avg_rtt_ms=2.1, packet_loss_pct=0.0, asn=None),
        RouteHop(ttl=2, address="81.19.104.1", hostname="hop2.isp-gw.net", avg_rtt_ms=9.8, packet_loss_pct=0.0, asn="AS6830"),
        RouteHop(ttl=3, address="195.66.224.14", hostname="linx-gw3.linx.net", avg_rtt_ms=14.2, packet_loss_pct=0.0, asn="AS5459"),
        RouteHop(ttl=4, address="151.101.1.140", hostname=None, avg_rtt_ms=15.9, packet_loss_pct=0.0, asn="AS54113"),
    ]

    console = Console(record=True, width=92, force_terminal=False, color_system="truecolor")
    console.print()
    console.print("[bold]Route to fastly.com[/] [dim]· 4 hops · stability: stable[/]")
    console.print(route_table(hops))
    svg = console.export_svg(title="netscope route")
    (IMAGES / "cli-route.svg").write_text(svg, encoding="utf-8")
    print("cli-route.svg written")


async def tui_screenshot() -> None:
    scores = [88.5, 92.0, 84.2, 95.0, 91.0]

    def scripted_round(container, config, gateway):
        score = scores[min(call_count["n"], len(scores) - 1)]
        call_count["n"] += 1
        measurements, experience, diagnosis = run_measurement_round(container, config, gateway=gateway)
        experience.score, experience.level = score, ExperienceLevel.GOOD
        return measurements, experience, diagnosis

    call_count = {"n": 0}
    app = NetScopeTUI(container=fake_container(), config=NetScopeConfig(), interval=3600.0, round_fn=scripted_round)
    async with app.run_test(size=(104, 30)) as pilot:
        for _ in range(len(scores)):
            app.request_round()
            await pilot.pause(0.25)
        await pilot.pause()
        svg = app.export_screenshot(title="netscope tui")
    (IMAGES / "tui-dashboard.svg").write_text(svg, encoding="utf-8")
    print("tui-dashboard.svg written")


if __name__ == "__main__":
    IMAGES.mkdir(parents=True, exist_ok=True)
    cli_screenshot()
    route_screenshot()
    asyncio.run(tui_screenshot())
