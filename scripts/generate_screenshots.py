"""Generate SVG screenshots for the README:
1. CLI diagnose output (rich Console export_svg) with fake healthy data.
2. CLI route output with a plausible hop table.
3. CLI speed output (the v0.3 throughput headline panel).
4. TUI dashboard (Textual export_screenshot) driven with fake data.
5. CLI drift output (v0.18) -- the REAL `run_drift_command` path, against
   a throwaway repository seeded with two plausible history windows.

Every screenshot renders through the real ui/render.py functions (the
drift one through the whole CLI command), so the README shows the
product's actual output format with scripted, plausible inputs -- the
inputs are openly fake, the rendering is the shipped code.

Run:  python scripts/generate_screenshots.py
Outputs: docs/images/cli-diagnose.svg, cli-route.svg, cli-speed.svg,
tui-dashboard.svg, cli-drift.svg
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
from netscope.ui.render import diagnosis_panel, measurements_table, route_table, speed_panel
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


def speed_screenshot() -> None:
    """The `netscope speed` headline panel with a plausible 6-second
    measurement (12 samples at the 0.5 s cadence, median below peak)
    and the v0.4 baseline-relative verdict attached, so the README
    shows the feature, not just the number. Rendered exactly like the
    command prints it: the panel only."""
    measurement = RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target="https://speed.cloudflare.com/__down?bytes=104857600",
        success=True,
        latency_ms=None,
        extra={
            "throughput_mbps": 94.6,
            "peak_mbps": 102.3,
            "bytes_transferred": 70_950_000,
            "duration_s": 6.0,
            "sample_count": 12,
            "baseline_mean_mbps": 82.1,
            "baseline_samples": 11,
            "deviation_pct": 15.2,
            "verdict": "faster_than_usual",
        },
    )

    console = Console(record=True, width=92, force_terminal=False, color_system="truecolor")
    console.print()
    console.print(speed_panel(measurement))
    svg = console.export_svg(title="netscope speed")
    (IMAGES / "cli-speed.svg").write_text(svg, encoding="utf-8")
    print("cli-speed.svg written")


def drift_screenshot() -> None:
    """The `netscope drift` verdict table (v0.15/v0.18): produced by
    running the REAL `run_drift_command` against a throwaway SQLite
    repository seeded with two windows of plausible history -- a line
    that quietly rots between the older and the recent window, so the
    watch fires on every vital, exactly the story the README tells.
    Timestamps are anchored to real clock time so the windows are
    internally consistent; the input data is scripted, the rendering
    is the shipped command."""
    import tempfile
    from datetime import UTC, datetime, timedelta

    from netscope.persistence.measurement_repository import MeasurementRepository
    from netscope.ui import render as ns_render
    from netscope.ui.cli import run_drift_command

    now = datetime.now(UTC)
    repo = MeasurementRepository.open(Path(tempfile.mkdtemp()) / "demo-drift.db")

    def sweep(age_days: float, latency: float, jitter: float, loss: float) -> RawMeasurement:
        return RawMeasurement(
            probe_type=ProbeType.ICMP,
            target="example.com",
            timestamp=now - timedelta(days=age_days),
            success=True,
            latency_ms=latency,
            jitter_ms=jitter,
            packet_loss_pct=loss,
        )

    def download(age_days: float, mbps: float) -> RawMeasurement:
        return RawMeasurement(
            probe_type=ProbeType.BANDWIDTH,
            target="example.com",
            timestamp=now - timedelta(days=age_days),
            success=True,
            extra={"throughput_mbps": mbps},
        )

    measurements: list[RawMeasurement] = []
    # Older window: a healthy line -- fast, steady, lossless.
    measurements += [sweep(10.0 - i * 0.3, 8.2 + i * 0.2, 1.1 + i * 0.05, 0.0) for i in range(6)]
    measurements += [download(9.5, 92.0), download(8.7, 90.5), download(8.1, 93.2), download(7.6, 91.4), download(7.1, 90.8)]
    # Recent window: the same line, quietly rotted -- slower, wobbling, dropping.
    measurements += [sweep(2.5 - i * 0.3, 13.9 + i * 0.2, 2.9 + i * 0.08, 2.0 + i * 0.2) for i in range(6)]
    measurements += [download(2.3, 71.5), download(1.6, 69.8), download(0.9, 72.4), download(0.6, 70.9), download(0.3, 71.2)]
    for m in measurements:
        repo.save(m)

    container = Container(probe_registry=ProbeRegistry(probes={}), measurement_repository=repo)

    record = Console(record=True, width=92, force_terminal=False, color_system="truecolor")
    original_console = ns_render._console
    ns_render._console = record
    try:
        run_drift_command(container, "example.com", days=7)
    finally:
        ns_render._console = original_console

    svg = record.export_svg(title="netscope drift")
    (IMAGES / "cli-drift.svg").write_text(svg, encoding="utf-8")
    print("cli-drift.svg written")


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
    speed_screenshot()
    asyncio.run(tui_screenshot())
