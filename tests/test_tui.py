"""
TUI smoke + behavior tests (tests/test_tui.py).

The TUI is tested fully offline: a fake round_fn returns scripted
measurements/experience/diagnosis, so no real probe ever runs and no
wall-clock interval matters (interval is set to an hour; the test
calls request_round() directly and waits for the RoundDone message to
land). Requires textual (installed with the dev extra) -- skipped
cleanly otherwise.
"""

from __future__ import annotations

import pytest

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.app.gateway import resolve_gateway
from netscope.core.models import (
    Diagnosis,
    Evidence,
    ExperienceEvent,
    ExperienceLevel,
    Hypothesis,
    ProbeType,
    RawMeasurement,
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


def _fake_container() -> Container:
    probes = {p: _FakeProbe(p) for p in (ProbeType.ICMP, ProbeType.DNS, ProbeType.HTTP, ProbeType.TCP, ProbeType.TLS, ProbeType.TRACEROUTE)}
    return Container(probe_registry=ProbeRegistry(probes=probes))


textual = pytest.importorskip("textual")

from netscope.ui.tui.app import NetScopeTUI  # noqa: E402 -- after importorskip


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
