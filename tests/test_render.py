"""Tests for netscope.ui.render's speed panel -- the user-facing side
of the v0.4 baseline-relative verdict: the comparison line appears
exactly when the use case attached one, with a human-readable label.
v0.8 adds the route-memory wording tests: one shared stability verdict
(`route_stability_summary`) and one hop-diff line (`format_route_change`)
behind both the CLI and the TUI."""

from __future__ import annotations

import io
from datetime import UTC, datetime

from rich.console import Console

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement, RouteHop, RouteSnapshot
from netscope.core.routing import RouteChange, RouteHopChange, analyze_route_churn
from netscope.ui.render import format_loss_note, format_route_change, measurements_table, route_stability_summary, sparkline, speed_panel

_STAMP = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


def _speed_measurement(extra: dict) -> RawMeasurement:
    return RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target="https://speed.example/down",
        success=True,
        extra=extra,
    )


def test_speed_panel_renders_the_verdict_line_when_attached():
    panel = speed_panel(
        _speed_measurement(
            {
                "throughput_mbps": 73.6,
                "peak_mbps": 88.0,
                "baseline_mean_mbps": 90.0,
                "baseline_samples": 4,
                "deviation_pct": -18.2,
                "verdict": "slower_than_usual",
            }
        )
    )
    plain = panel.renderable.plain
    assert "vs your usual 90.0 Mbps" in plain
    assert "-18.2%" in plain
    assert "slower than usual" in plain


def test_speed_panel_stays_silent_while_the_baseline_is_young():
    panel = speed_panel(_speed_measurement({"throughput_mbps": 87.4, "peak_mbps": 94.7}))
    assert "vs your usual" not in panel.renderable.plain


def test_speed_panel_failure_path_is_unchanged():
    panel = speed_panel(
        RawMeasurement(
            probe_type=ProbeType.BANDWIDTH,
            target="https://speed.example/down",
            success=False,
            error="HTTP 404",
            error_type=ProbeErrorType.HTTP_FAILURE,
        )
    )
    assert "speed test failed" in panel.renderable.plain


# ---------------------------------------------------------------------------
# Route memory wording (v0.8) -- one shared verdict, two densities
# ---------------------------------------------------------------------------


def _hop(address: str | None, *, ttl: int = 1) -> RouteHop:
    return RouteHop(ttl=ttl, address=address, hostname=None, avg_rtt_ms=10.0, packet_loss_pct=0.0)


def _changed_route_change() -> RouteChange:
    return RouteChange(
        previous_signature="10.0.0.1|10.0.0.2|10.0.0.3",
        new_signature="10.0.0.1|10.0.0.2|10.0.0.9",
        previous_timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        new_timestamp=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        changed_hops=[RouteHopChange(ttl=3, previous_address="10.0.0.3", new_address="10.0.0.9")],
    )


def test_format_route_change_names_the_moved_hop():
    line = format_route_change(_changed_route_change())
    assert line == "hop 3: 10.0.0.3 → 10.0.0.9"


def test_format_route_change_reads_a_dropped_hop_as_no_reply():
    change = RouteChange(
        previous_signature="10.0.0.1|10.0.0.2",
        new_signature="10.0.0.1|*",
        previous_timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        new_timestamp=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        changed_hops=[RouteHopChange(ttl=2, previous_address="10.0.0.2", new_address=None)],
    )
    assert format_route_change(change) == "hop 2: 10.0.0.2 → no reply"


def test_format_route_change_caps_at_three_hops_then_discloses_the_rest():
    change = RouteChange(
        previous_signature="a|b|c|d|e",
        new_signature="a|x|y|z|w",
        previous_timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        new_timestamp=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        changed_hops=[
            RouteHopChange(ttl=2, previous_address="b", new_address="x"),
            RouteHopChange(ttl=3, previous_address="c", new_address="y"),
            RouteHopChange(ttl=4, previous_address="d", new_address="z"),
            RouteHopChange(ttl=5, previous_address="e", new_address="w"),
            RouteHopChange(ttl=6, previous_address="f", new_address="v"),
        ],
    )
    line = format_route_change(change)
    assert line.startswith("hop 2: b → x, hop 3: c → y, hop 4: d → z")
    assert line.endswith("(+2 more)")


def test_stability_summary_first_observation_is_never_called_stable():
    churn = analyze_route_churn([RouteSnapshot(target="example.com", timestamp=_STAMP, hops=[_hop("10.0.0.1")])])
    assert "first observation" in route_stability_summary(churn, prior_runs=0)
    assert "stable" not in route_stability_summary(churn, prior_runs=0)


def test_stability_summary_stable_across_runs_names_the_window():
    start = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    churn = analyze_route_churn(
        [
            RouteSnapshot(target="example.com", timestamp=start, hops=[_hop("10.0.0.1")]),
            RouteSnapshot(target="example.com", timestamp=_STAMP, hops=[_hop("10.0.0.1")]),
        ]
    )
    line = route_stability_summary(churn, prior_runs=1)
    assert "unchanged across the last 2 runs" in line
    assert "same path since 2026-11-09 10:00:00" in line


def test_stability_summary_changed_leads_with_when_and_what():
    previous = RouteSnapshot(
        target="example.com",
        timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        hops=[_hop("10.0.0.1"), _hop("10.0.0.3", ttl=2)],
    )
    current = RouteSnapshot(
        target="example.com",
        timestamp=_STAMP,
        hops=[_hop("10.0.0.1"), _hop("10.0.0.9", ttl=2)],
    )
    churn = analyze_route_churn([previous, current])
    line = route_stability_summary(churn, prior_runs=1)
    assert line == "changed vs 2026-11-09 10:00:00 — hop 2: 10.0.0.3 → 10.0.0.9"


def test_stability_summary_compact_is_the_tui_density():
    previous = RouteSnapshot(
        target="example.com",
        timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        hops=[_hop("10.0.0.1"), _hop("10.0.0.3", ttl=2)],
    )
    current = RouteSnapshot(
        target="example.com",
        timestamp=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        hops=[_hop("10.0.0.1"), _hop("10.0.0.9", ttl=2)],
    )
    churn = analyze_route_churn([previous, current])
    assert route_stability_summary(churn, prior_runs=1, compact=True) == "changed vs 11-09 10:00 — hop 2: 10.0.0.3 → 10.0.0.9"

    stable = analyze_route_churn([current, current])
    assert route_stability_summary(stable, prior_runs=2, compact=True) == "stable across 2 runs"


# ---------------------------------------------------------------------------
# Loss-confirmation wording (v0.9) -- one shared verdict for the detail
# column, exactly like the route memory's shared stability wording.
# ---------------------------------------------------------------------------


def _merged_loss_measurement(extra: dict) -> RawMeasurement:
    return RawMeasurement(
        probe_type=ProbeType.ICMP,
        target="1.1.1.1",
        success=True,
        latency_ms=12.5,
        packet_loss_pct=8.3,
        extra=extra,
    )


def test_loss_note_says_confirmed_when_the_loss_reproduced():
    m = _merged_loss_measurement({"loss_rounds": [50.0, 0.0, 25.0], "loss_confirmed": True, "loss_window_rounds": 3})
    assert format_loss_note(m) == "loss confirmed across 3 sweeps"


def test_loss_note_says_transient_when_confirmations_came_back_clean():
    m = _merged_loss_measurement({"loss_rounds": [25.0, 0.0, 0.0], "loss_confirmed": False, "loss_window_rounds": 3})
    assert format_loss_note(m) == "loss transient across 3 sweeps"


def test_loss_note_is_empty_for_a_plain_sweep():
    assert format_loss_note(_merged_loss_measurement({})) == ""


def test_measurements_table_detail_carries_the_loss_note():
    out = io.StringIO()
    console = Console(file=out, width=160, force_terminal=False)
    console.print(measurements_table([_merged_loss_measurement({"loss_rounds": [25.0, 0.0], "loss_confirmed": False, "loss_window_rounds": 2})]))
    assert "loss transient across 2 sweeps" in out.getvalue()


# ---------------------------------------------------------------------------
# Score sparkline (v0.10) -- one line, eight blocks, no chart dependency.
# ---------------------------------------------------------------------------


def test_sparkline_maps_a_rising_series_onto_rising_blocks():
    blocks = "▁▂▃▄▅▆▇█"
    out = sparkline([0.0, 25.0, 50.0, 75.0, 100.0])
    assert len(out) == 5
    assert [blocks.index(ch) for ch in out] == sorted(blocks.index(ch) for ch in out)
    assert out[0] == "▁" and out[-1] == "█"


def test_sparkline_normalizes_to_its_own_range():
    # A trend between 91 and 94 must read like one between 20 and 95:
    # the sparkline answers "how did it move", not "how bad is it".
    assert sparkline([91.0, 92.5, 94.0]) == sparkline([20.0, 21.5, 23.0])


def test_sparkline_flat_series_renders_the_mid_block():
    assert sparkline([80.0, 80.0, 80.0]) == "▅▅▅"


def test_sparkline_skips_unscorable_rounds_instead_of_flattening_them():
    # None is absence of evidence, not a low score.
    assert sparkline([40.0, None, 80.0]) == sparkline([40.0, 80.0])


def test_sparkline_needs_two_scorable_points_to_be_a_trend():
    assert sparkline([]) == ""
    assert sparkline([None]) == ""
    assert sparkline([50.0]) == ""
    assert sparkline([50.0, None]) == ""
