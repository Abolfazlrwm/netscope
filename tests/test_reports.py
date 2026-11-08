"""
Tests for the v0.5 evidence report (TASK-039).

Per future-roadmap.md's TASK-039 row: "Unit tests for report
content/structure." The report only ever claims what the persisted log
proves, so these tests assert exactly that contract: window and totals
math, per-target aggregation (failures, verdict counts, latency means
over successful readings only), disclosed truncation, the honest empty
case, and both renderings (markdown document + `--json` payload) plus
the CLI command's stdout/file behavior.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from netscope.app.container import Container
from netscope.app.reports import evidence_report_markdown, serialize_evidence_report
from netscope.app.use_cases import (
    REPORT_MAX_FAILURES,
    REPORT_MAX_ROUNDS,
    REPORT_ROWS_PER_TARGET,
    build_evidence_report,
)
from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement
from netscope.persistence.measurement_repository import MeasurementRepository
from netscope.ui.cli import build_arg_parser, run_report_command

UNTIL = datetime(2026, 11, 8, 22, 0, 0, tzinfo=UTC)


def _repo() -> MeasurementRepository:
    return MeasurementRepository.open(":memory:")


def _m(**overrides) -> RawMeasurement:
    defaults = dict(
        probe_type=ProbeType.ICMP,
        target="gateway.local",
        timestamp=UNTIL - timedelta(hours=1),
        success=True,
        latency_ms=12.5,
        packet_loss_pct=0.0,
    )
    defaults.update(overrides)
    return RawMeasurement(**defaults)


def _container(repo: MeasurementRepository | None = None) -> Container:
    return Container(
        probe_registry=None,  # type: ignore[arg-type]  -- report never probes
        measurement_repository=repo,
    )


def _seed_two_rounds(repo: MeasurementRepository) -> None:
    """Two diagnose-shaped rounds one hour apart: 3 checks each, one
    dns failure in the second round, plus one bandwidth run carrying a
    v0.4 verdict."""
    base = UNTIL - timedelta(days=1)
    first = [
        _m(timestamp=base, latency_ms=10.0),
        _m(probe_type=ProbeType.DNS, target="dns.example", timestamp=base + timedelta(seconds=2), latency_ms=8.0),
        _m(probe_type=ProbeType.HTTP, target="www.example", timestamp=base + timedelta(seconds=4), latency_ms=120.0),
    ]
    second = [
        _m(timestamp=base + timedelta(hours=1), latency_ms=40.0),
        _m(
            probe_type=ProbeType.DNS,
            target="dns.example",
            timestamp=base + timedelta(hours=1, seconds=2),
            success=False,
            error="no answer within 2.0s",
            error_type=ProbeErrorType.TIMEOUT,
        ),
        _m(probe_type=ProbeType.HTTP, target="www.example", timestamp=base + timedelta(hours=1, seconds=4), latency_ms=140.0),
    ]
    speed = _m(
        probe_type=ProbeType.BANDWIDTH,
        target="https://speed.example/down",
        timestamp=base + timedelta(hours=2),
        latency_ms=None,  # a download reading is throughput evidence, not latency
        extra={"throughput_mbps": 61.2, "verdict": "slower_than_usual"},
    )
    for m in [*first, *second, speed]:
        repo.save(m)


# ---------------------------------------------------------------------------
# build_evidence_report: window, totals, aggregation
# ---------------------------------------------------------------------------


def test_report_window_totals_and_targets():
    repo = _repo()
    _seed_two_rounds(repo)
    report = build_evidence_report(_container(repo), days=7, until=UNTIL)

    assert report.scope is None
    assert report.since == UNTIL - timedelta(days=7)
    assert report.until == UNTIL
    assert report.targets == ["dns.example", "gateway.local", "https://speed.example/down", "www.example"]
    assert report.measurements == 7
    assert report.failures == 1
    assert len(report.rounds) == 3  # two diagnose rounds + the standalone speed run


def test_report_window_excludes_measurements_outside_it():
    repo = _repo()
    _seed_two_rounds(repo)
    repo.save(_m(timestamp=UNTIL - timedelta(days=8)))  # outside the 7-day window
    report = build_evidence_report(_container(repo), days=7, until=UNTIL)
    assert report.measurements == 7

    report_9d = build_evidence_report(_container(repo), days=9, until=UNTIL)
    assert report_9d.measurements == 8


def test_report_scores_rounds_against_the_current_baseline():
    repo = _repo()
    _seed_two_rounds(repo)
    report = build_evidence_report(_container(repo), days=7, until=UNTIL)

    scored = [r for r in report.rounds if len(r.measurements) == 3]
    assert len(scored) == 2
    healthy, degraded = sorted(scored, key=lambda r: r.experience.score, reverse=True)
    assert healthy.experience.score > degraded.experience.score
    # The re-computed score is stamped with the round's own time, not now.
    assert degraded.experience.timestamp == degraded.timestamp
    # Mean over every scored round in the window -- including the
    # standalone speed run (which scores neutral: no latency evidence).
    assert report.mean_score == pytest.approx(sum(r.experience.score for r in report.rounds) / len(report.rounds))


def test_report_target_summary_aggregates_failures_verdicts_and_latency():
    repo = _repo()
    _seed_two_rounds(repo)
    report = build_evidence_report(_container(repo), days=7, until=UNTIL)

    dns = next(t for t in report.target_reports if t.target == "dns.example")
    assert dns.measurements == 2
    assert dns.failures == 1
    assert dns.mean_latency_ms == pytest.approx(8.0)  # successful readings only
    assert dns.probe_types == ["dns"]

    speed = next(t for t in report.target_reports if "speed.example" in t.target)
    assert speed.verdict_counts == {"slower_than_usual": 1}
    assert speed.mean_latency_ms is None  # a download reading never poses as latency

    gateway = next(t for t in report.target_reports if t.target == "gateway.local")
    assert gateway.verdict_counts == {}
    assert gateway.mean_latency_ms == pytest.approx((10.0 + 40.0) / 2)


def test_report_target_filter_scopes_everything():
    repo = _repo()
    _seed_two_rounds(repo)
    report = build_evidence_report(_container(repo), days=7, until=UNTIL, target="dns.example")

    assert report.scope == "dns.example"
    assert report.targets == ["dns.example"]
    assert report.measurements == 2
    assert report.failures == 1
    assert len(report.failure_rows) == 1


def test_report_days_must_be_positive():
    with pytest.raises(ValueError, match="days must be a positive integer"):
        build_evidence_report(_container(_repo()), days=0, until=UNTIL)


def test_report_without_a_repository_is_an_honest_empty_report():
    report = build_evidence_report(_container(None), days=7, until=UNTIL)
    assert report.measurements == 0
    assert report.targets == []
    assert report.rounds == []
    assert report.mean_score is None


def test_report_truncation_is_disclosed_not_silent():
    repo = _repo()
    for i in range(REPORT_ROWS_PER_TARGET + 6):
        repo.save(_m(timestamp=UNTIL - timedelta(hours=REPORT_ROWS_PER_TARGET + 12 - i)))
    report = build_evidence_report(_container(repo), days=7, until=UNTIL)

    gateway = report.target_reports[0]
    assert gateway.measurements == REPORT_ROWS_PER_TARGET + 6
    assert len(gateway.rows) == REPORT_ROWS_PER_TARGET
    assert gateway.truncated == 6


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------


def test_markdown_contains_the_evidence_sections():
    repo = _repo()
    _seed_two_rounds(repo)
    md = evidence_report_markdown(build_evidence_report(_container(repo), days=7, until=UNTIL))

    assert md.startswith("# NetScope evidence report")
    assert "## Rounds" in md
    assert "## Per-target evidence" in md
    assert "### dns.example" in md
    assert "## Failures" in md
    assert "## Notes" in md
    # The failure appears verbatim, with its taxonomy.
    assert "dns `dns.example`" in md
    assert "no answer within 2.0s" in md
    # The v0.4 verdict lives in the verdict column, never as latency.
    assert "verdicts: slower_than_usual ×1" in md
    assert "slower_than_usual |" in md
    # Absent metrics are absent, not zero.
    assert "| -- |" in md


def test_markdown_discloses_omissions():
    repo = _repo()
    for i in range(REPORT_ROWS_PER_TARGET + 6):
        repo.save(_m(timestamp=UNTIL - timedelta(hours=REPORT_ROWS_PER_TARGET + 12 - i)))
    md = evidence_report_markdown(build_evidence_report(_container(repo), days=7, until=UNTIL))
    assert "6 older row(s) omitted" in md

    repo2 = _repo()
    for i in range(REPORT_MAX_ROUNDS + 3):
        # Each round needs a >120s gap from the next; spread them 10 min apart.
        repo2.save(_m(timestamp=UNTIL - timedelta(minutes=10 * (REPORT_MAX_ROUNDS + 3 - i))))
    md2 = evidence_report_markdown(build_evidence_report(_container(repo2), days=7, until=UNTIL))
    assert "3 older round(s) omitted" in md2

    repo3 = _repo()
    for i in range(REPORT_MAX_FAILURES + 5):
        repo3.save(
            _m(
                success=False,
                error="timeout",
                error_type=ProbeErrorType.TIMEOUT,
                timestamp=UNTIL - timedelta(minutes=10 * (REPORT_MAX_FAILURES + 5 - i)),
            )
        )
    md3 = evidence_report_markdown(build_evidence_report(_container(repo3), days=7, until=UNTIL))
    assert "5 older failure(s) omitted" in md3


def test_markdown_empty_log_is_honest_about_it():
    md = evidence_report_markdown(build_evidence_report(_container(None), days=7, until=UNTIL))
    assert "No measurements in this window yet" in md
    assert "## Per-target evidence" not in md
    assert "mean score --" in md


def test_markdown_escapes_pipes_in_cells():
    repo = _repo()
    repo.save(_m(target="a|b.example", error="weird|error"))
    md = evidence_report_markdown(build_evidence_report(_container(repo), days=7, until=UNTIL))
    assert "a\\|b.example" in md
    assert "weird\\|error" in md


# ---------------------------------------------------------------------------
# JSON payload
# ---------------------------------------------------------------------------


def test_json_payload_shape_is_canonical_and_safe():
    repo = _repo()
    _seed_two_rounds(repo)
    payload = serialize_evidence_report(build_evidence_report(_container(repo), days=7, until=UNTIL))

    assert payload["tool"] == "netscope"
    assert payload["report"] == "evidence"
    assert payload["window"]["since"] == (UNTIL - timedelta(days=7)).isoformat()
    assert payload["totals"]["measurements"] == 7
    assert payload["totals"]["failures"] == 1
    assert payload["scope"] is None
    assert len(payload["rounds"]) == 3
    dns = next(t for t in payload["targets"] if t["target"] == "dns.example")
    assert dns["failures"] == 1
    assert dns["rows"][1]["error_type"] == "timeout"
    assert len(payload["failures"]) == 1
    # The whole payload survives json.dumps -- the JSON-safety contract.
    round_trip = json.loads(json.dumps(payload))
    assert round_trip["totals"]["measurements"] == 7


def test_json_payload_for_target_scope():
    repo = _repo()
    _seed_two_rounds(repo)
    payload = serialize_evidence_report(build_evidence_report(_container(repo), days=7, until=UNTIL, target="dns.example"))
    assert payload["scope"] == "dns.example"
    assert payload["totals"]["measurements"] == 2


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_argparser_has_the_report_arguments():
    args = build_arg_parser().parse_args(["report", "--days", "3", "--target", "1.1.1.1", "--out", "r.md"])
    assert args.command == "report"
    assert args.days == 3
    assert args.target == "1.1.1.1"
    assert args.out == "r.md"
    assert args.json is False

    default_args = build_arg_parser().parse_args(["report"])
    assert default_args.days == 7
    assert default_args.target is None


def test_report_command_stdout(capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    code = run_report_command(_container(repo), days=7)
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("# NetScope evidence report")
    assert "## Per-target evidence" in out


def test_report_command_writes_a_utf8_file(tmp_path, capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    out_file = tmp_path / "evidence.md"
    code = run_report_command(_container(repo), days=7, out=str(out_file))
    assert code == 0

    text = out_file.read_text(encoding="utf-8")
    assert text.startswith("# NetScope evidence report")
    assert "→" in text  # multi-byte glyphs survive the round-trip
    assert "measurement(s) written" not in text
    assert "Evidence report (7 measurement(s), 1 failed) written to" in capsys.readouterr().out


def test_report_command_json_output(capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    code = run_report_command(_container(repo), days=7, json_output=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"] == "evidence"
    assert payload["totals"]["measurements"] == 7


def test_report_command_rejects_non_positive_days(capsys):
    code = run_report_command(_container(None), days=0)
    assert code == 2
    assert "days must be a positive integer" in capsys.readouterr().out


def test_repository_targets_lists_distinct_targets_alphabetically():
    repo = _repo()
    repo.save(_m(target="zeta.example"))
    repo.save(_m(target="alpha.example"))
    repo.save(_m(target="alpha.example", timestamp=UNTIL))
    assert repo.targets() == ["alpha.example", "zeta.example"]
