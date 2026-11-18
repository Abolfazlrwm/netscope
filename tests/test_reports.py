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
from netscope.app.reports import (
    evidence_report_csv,
    evidence_report_html,
    evidence_report_markdown,
    evidence_report_prometheus,
    history_rounds_csv,
    serialize_evidence_report,
)
from netscope.app.use_cases import (
    REPORT_MAX_FAILURES,
    REPORT_MAX_ROUNDS,
    REPORT_ROWS_PER_TARGET,
    build_evidence_report,
    recent_rounds,
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


def _seed_two_rounds(repo: MeasurementRepository, until: datetime = UNTIL) -> None:
    """Two diagnose-shaped rounds one hour apart: 3 checks each, one
    dns failure in the second round, plus one bandwidth run carrying a
    v0.4 verdict.

    All seed timestamps derive from `until`, never from wall-clock now.
    The use-case tests pin `until=UNTIL` on `build_evidence_report`, and
    the CLI wiring tests pin the command's own `until`, which
    `run_report_command` passes straight through. Seeds and window end
    must age TOGETHER, not one without the other -- that is what keeps
    the suite date-immune instead of quietly falling out of the 7-day
    window one week after authoring.
    """
    base = until - timedelta(days=1)
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
    code = run_report_command(_container(repo), days=7, until=UNTIL)
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("# NetScope evidence report")
    assert "## Per-target evidence" in out


def test_report_command_writes_a_utf8_file(tmp_path, capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    out_file = tmp_path / "evidence.md"
    code = run_report_command(_container(repo), days=7, out=str(out_file), until=UNTIL)
    assert code == 0

    text = out_file.read_text(encoding="utf-8")
    assert text.startswith("# NetScope evidence report")
    assert "→" in text  # multi-byte glyphs survive the round-trip
    assert "measurement(s) written" not in text
    assert "Evidence report (7 measurement(s), 1 failed) written to" in capsys.readouterr().out


def test_report_command_json_output(capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    code = run_report_command(_container(repo), days=7, json_output=True, until=UNTIL)
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


# ---------------------------------------------------------------------------
# HTML rendering (v0.10): the same facts, dressed for humans. Same caps,
# same disclosures, same "--" for absent metrics -- and a document that
# is provably self-contained: no scripts, no external assets.
# ---------------------------------------------------------------------------


def _html(repo):
    return evidence_report_html(build_evidence_report(_container(repo), days=7, until=UNTIL))


def test_html_contains_the_evidence_sections():
    repo = _repo()
    _seed_two_rounds(repo)
    page = _html(repo)

    assert page.startswith("<!doctype html>")
    assert "NetScope evidence report" in page
    assert "dns.example" in page
    assert "Rounds" in page
    assert "Per-target evidence" in page
    assert "Failures" in page
    assert "Notes" in page
    # The failure appears verbatim, with its taxonomy.
    assert "no answer within 2.0s" in page
    # The v0.4 verdict lives in the verdict column, never as latency.
    assert "verdicts: slower_than_usual ×1" in page
    # Absent metrics are absent, not zero.
    assert "--" in page


def test_html_is_self_contained_no_scripts_or_external_assets():
    repo = _repo()
    _seed_two_rounds(repo)
    page = _html(repo)

    assert "<script" not in page
    assert "<link" not in page
    assert "<img" not in page
    assert 'src="http' not in page
    assert 'href="http' not in page
    assert "<style>" in page  # the one stylesheet rides inline


def test_html_escapes_user_controlled_text():
    repo = _repo()
    hostile = '"><script>alert(1)</script>'
    repo.save(_m(target=hostile, timestamp=UNTIL - timedelta(hours=1)))
    page = _html(repo)

    # The hostile target appears only as escaped text, never as markup.
    assert "<script>alert(1)</script>" not in page.replace("&#x27;", "'")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_html_discloses_omissions():
    repo = _repo()
    for i in range(REPORT_ROWS_PER_TARGET + 6):
        repo.save(_m(timestamp=UNTIL - timedelta(hours=REPORT_ROWS_PER_TARGET + 12 - i)))
    page = _html(repo)
    assert "6 older row(s) omitted" in page

    repo2 = _repo()
    for i in range(REPORT_MAX_ROUNDS + 3):
        repo2.save(_m(timestamp=UNTIL - timedelta(minutes=10 * (REPORT_MAX_ROUNDS + 3 - i))))
    page2 = _html(repo2)
    assert "3 older round(s) omitted" in page2


def test_html_empty_log_is_honest_about_it():
    page = _html(_repo())
    assert "No measurements in this window yet" in page
    assert "Per-target evidence" not in page
    assert "mean score" in page


def test_html_score_trend_needs_two_scored_rounds():
    repo = _repo()
    repo.save(_m(timestamp=UNTIL - timedelta(hours=1)))
    page = _html(repo)
    assert "Not enough scored rounds for a trend" in page
    assert "<polyline" not in page

    repo2 = _repo()
    _seed_two_rounds(repo2)
    page2 = _html(repo2)
    assert "<polyline" in page2  # the rounds sparkline
    assert "score trend, oldest → newest" in page2
    assert "stroke-dasharray" in page2  # and the mean-score gauge


def test_html_latency_sparkline_uses_only_shown_rows():
    repo = _repo()
    for i in range(REPORT_ROWS_PER_TARGET + 6):
        repo.save(_m(latency_ms=10.0 + i, timestamp=UNTIL - timedelta(hours=REPORT_ROWS_PER_TARGET + 12 - i)))
    page = _html(repo)
    # The sparkline describes the SHOWN rows (40 points), not the window.
    assert page.count(",") >= 40  # polyline coordinate pairs exist
    assert "latency per shown measurement" in page


# ---------------------------------------------------------------------------
# `netscope report --html FILE` (v0.10): the CLI writes the same
# self-contained page the use case renders -- UTF-8, confirmation line,
# and the JSON short-circuit keeps winning, as documented.
# ---------------------------------------------------------------------------


def test_argparser_has_the_html_report_argument():
    args = build_arg_parser().parse_args(["report", "--html", "out.html"])
    assert args.html == "out.html"


def test_report_command_writes_the_html_file(tmp_path, capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    out_path = tmp_path / "report.html"

    exit_code = run_report_command(_container(repo), days=7, html_out=str(out_path), until=UNTIL)

    assert exit_code == 0
    page = out_path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "NetScope evidence report" in page
    assert "HTML report" in capsys.readouterr().out


def test_report_command_html_and_markdown_can_coexist(tmp_path, capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    md_path = tmp_path / "report.md"
    html_path = tmp_path / "report.html"

    exit_code = run_report_command(_container(repo), days=7, out=str(md_path), html_out=str(html_path), until=UNTIL)

    assert exit_code == 0
    assert md_path.read_text(encoding="utf-8").startswith("# NetScope evidence report")
    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_report_command_json_output_ignores_html(tmp_path, capsys):
    repo = _repo()
    _seed_two_rounds(repo)
    html_path = tmp_path / "report.html"

    exit_code = run_report_command(_container(repo), days=7, html_out=str(html_path), json_output=True, until=UNTIL)

    assert exit_code == 0
    assert not html_path.exists()  # the JSON short-circuit, unchanged
    out = capsys.readouterr().out
    assert '"report": "evidence"' in out


# ---------------------------------------------------------------------------
# Jitter columns (v0.13) -- the third vital sign rides along wherever the
# evidence tables do
# ---------------------------------------------------------------------------


def test_report_rows_carry_and_render_jitter():
    repo = _repo()
    repo.save(_m(jitter_ms=3.2, timestamp=UNTIL - timedelta(hours=2)))
    repo.save(_m(jitter_ms=None, timestamp=UNTIL - timedelta(hours=1)))

    report = build_evidence_report(_container(repo), days=7, until=UNTIL)
    target = report.target_reports[0]
    assert [row.jitter_ms for row in target.rows] == [3.2, None]

    md = evidence_report_markdown(report)
    assert "jitter_ms" in md
    assert "| 3.2 |" in md
    assert "| -- |" in md  # a reading that never existed stays absent, not zero

    payload = serialize_evidence_report(report)
    assert [row["jitter_ms"] for row in payload["targets"][0]["rows"]] == [3.2, None]

    page = evidence_report_html(report)
    assert "jitter_ms" in page
    assert "<td class='num'>3.2</td>" in page
    assert "<td class='num'>--</td>" in page


# ---------------------------------------------------------------------------
# History CSV (v0.17) -- `netscope history --csv`: one row per stored
# measurement, its round denormalized onto every row. Rendered through
# the same seeded-log path the history command itself takes, so the
# test proves the exact wire the file gets.
# ---------------------------------------------------------------------------


def _csv_rows(text: str) -> list[list[str]]:
    import csv as _csv
    import io as _io

    return list(_csv.reader(_io.StringIO(text)))


def test_history_csv_header_and_one_row_per_measurement():
    repo = _repo()
    _seed_two_rounds(repo)
    container = _container(repo)

    text = history_rounds_csv(recent_rounds(container, limit=10))
    rows = _csv_rows(text)

    assert rows[0] == [
        "round_timestamp",
        "round_score",
        "round_level",
        "timestamp",
        "probe_type",
        "target",
        "success",
        "latency_ms",
        "packet_loss_pct",
        "jitter_ms",
        "error_type",
        "error",
    ]
    # 7 seeded measurements -> 7 data rows; one per measurement, never
    # one per round and never a round-level summary row.
    assert len(rows) == 8


def test_history_csv_denormalizes_the_round_onto_every_row():
    repo = _repo()
    _seed_two_rounds(repo)
    container = _container(repo)

    rounds = recent_rounds(container, limit=10)
    text = history_rounds_csv(rounds)
    rows = _csv_rows(text)[1:]

    # Every row of a round carries that round's timestamp -- the join
    # key a spreadsheet groups by.
    first_round_rows = [r for r in rows if r[0] == rounds[0].timestamp.isoformat()]
    assert len(first_round_rows) == 3
    assert {r[4] for r in first_round_rows} == {"icmp", "dns", "http"}

    # The round's recomputed score rides along; an unscored round (no
    # baseline yet) would leave the cell empty instead of faking one.
    if rounds[0].experience is not None:
        assert all(r[1] != "" for r in first_round_rows)
    else:
        assert all(r[1] == "" for r in first_round_rows)


def test_history_csv_failed_reading_and_absent_metrics_are_honest():
    repo = _repo()
    _seed_two_rounds(repo)
    container = _container(repo)

    text = history_rounds_csv(recent_rounds(container, limit=10))
    rows = _csv_rows(text)[1:]

    dns_failure = next(r for r in rows if r[4] == "dns" and r[6] == "false")
    assert dns_failure[10] == "timeout"
    assert dns_failure[11] == "no answer within 2.0s"

    # A successful ping: numbers present, error cells empty (never "--").
    icmp_ok = next(r for r in rows if r[4] == "icmp" and r[6] == "true")
    assert icmp_ok[7] == "10.0"
    assert icmp_ok[10] == ""
    assert icmp_ok[11] == ""

    # The bandwidth reading measured no latency: the cell stays empty --
    # absence as the log proves it, not zero dressed up as a reading.
    bandwidth = next(r for r in rows if r[4] == "bandwidth")
    assert bandwidth[7] == ""


def test_history_csv_quoting_keeps_commas_inside_their_cell():
    repo = _repo()
    repo.save(_m(success=False, error="resolver said: SERVFAIL, retry 2", error_type=ProbeErrorType.DNS_FAILURE))
    container = _container(repo)

    text = history_rounds_csv(recent_rounds(container, limit=10))
    rows = _csv_rows(text)[1:]

    assert rows[0][11] == "resolver said: SERVFAIL, retry 2"


def test_history_csv_empty_log_is_header_only():
    container = _container(_repo())

    text = history_rounds_csv(recent_rounds(container, limit=10))

    assert text == (
        "round_timestamp,round_score,round_level,timestamp,probe_type,target,success,latency_ms,packet_loss_pct,jitter_ms,error_type,error\n"
    )


# ---------------------------------------------------------------------------
# Evidence CSV (v0.17) -- the evidence tables flattened to one row per
# shown reading, the row's own persisted geo/asn enrichment unfolded
# onto scalar columns.
# ---------------------------------------------------------------------------


def test_evidence_csv_one_row_per_shown_reading():
    repo = _repo()
    _seed_two_rounds(repo)

    text = evidence_report_csv(build_evidence_report(_container(repo), days=7, until=UNTIL))
    rows = _csv_rows(text)

    assert rows[0] == [
        "target",
        "timestamp",
        "probe_type",
        "success",
        "latency_ms",
        "packet_loss_pct",
        "jitter_ms",
        "verdict",
        "error_type",
        "error",
        "city",
        "country",
        "distance_km",
        "asn",
        "organization",
    ]
    # The seeded log has 7 measurements and no truncation, so the CSV
    # carries exactly 7 data rows -- the same slice the tables render.
    assert len(rows) == 8

    # A failed reading is evidence too: it rides along with its error
    # columns filled, success false.
    dns_failure = next(r for r in rows if r[2] == "dns" and r[3] == "false")
    assert dns_failure[8] == "timeout"
    assert dns_failure[9] == "no answer within 2.0s"

    # The bandwidth run's verdict travels in its own column, never
    # smeared into a latency cell.
    speed = next(r for r in rows if r[2] == "bandwidth")
    assert speed[7] == "slower_than_usual"
    assert speed[4] == ""


def test_evidence_csv_flattens_persisted_geo_and_asn_onto_their_row():
    repo = _repo()
    repo.save(_m(timestamp=UNTIL - timedelta(hours=2)))  # the bare neighbor: gateway.local, no enrichment
    repo.save(
        _m(
            probe_type=ProbeType.TCP,
            target="edge.example",
            extra={
                "geo": {"city": "Marseille", "country": "France", "distance_km": 4210.5},
                "asn": {"asn": 13335, "organization": "Cloudflare, Inc."},
            },
        )
    )

    rows = _csv_rows(evidence_report_csv(build_evidence_report(_container(repo), days=7, until=UNTIL)))[1:]

    enriched = next(r for r in rows if r[0] == "edge.example")
    assert enriched[10] == "Marseille"
    assert enriched[11] == "France"
    assert enriched[12] == "4210.5"
    assert enriched[13] == "13335"
    assert enriched[14] == "Cloudflare, Inc."  # comma survives, quoted by the csv module

    # The other targets' rows never borrowed enrichment they did not
    # record: their geo/asn cells are empty, not repeated from a
    # neighbor.
    bare = next(r for r in rows if r[0] == "gateway.local")
    assert bare[10] == ""
    assert bare[13] == ""


def test_evidence_csv_empty_log_is_header_only():
    text = evidence_report_csv(build_evidence_report(_container(_repo()), days=7, until=UNTIL))

    assert text.endswith("city,country,distance_km,asn,organization\n")
    assert len(_csv_rows(text)) == 1


# ---------------------------------------------------------------------------
# Evidence Prometheus (v0.17) -- the textfile-collector dialect: report
# totals, per-target aggregates, latest successful per-check readings.
# ---------------------------------------------------------------------------


def _prom_samples(text: str, metric: str) -> list[str]:
    """The sample lines of one metric family, comments stripped."""
    return [line for line in text.splitlines() if line.startswith(metric)]


def test_prometheus_totals_and_means_match_the_report():
    repo = _repo()
    _seed_two_rounds(repo)
    report = build_evidence_report(_container(repo), days=7, until=UNTIL)

    text = evidence_report_prometheus(report)

    assert text.startswith("# NetScope evidence window ")
    assert f"netscope_report_measurements {report.measurements}" in text
    assert f"netscope_report_failures {report.failures}" in text
    assert f"netscope_report_targets {len(report.target_reports)}" in text
    assert f"netscope_report_mean_score {report.mean_score:g}" in text
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            assert line.endswith(" gauge")


def test_prometheus_latest_per_check_reading_wins():
    repo = _repo()
    _seed_two_rounds(repo)  # gateway.local icmp: 10.0 then 40.0

    text = evidence_report_prometheus(build_evidence_report(_container(repo), days=7, until=UNTIL))

    icmp_lines = [line for line in _prom_samples(text, "netscope_check_latency_ms") if 'check="icmp"' in line and 'target="gateway.local"' in line]
    assert icmp_lines == ['netscope_check_latency_ms{target="gateway.local",check="icmp"} 40']

    # The mean is a different family with a different label shape --
    # never a stray single-label sample inside the per-check one.
    mean_lines = [line for line in _prom_samples(text, "netscope_target_mean_latency_ms") if 'target="gateway.local"' in line]
    assert mean_lines == ['netscope_target_mean_latency_ms{target="gateway.local"} 25']


def test_prometheus_omits_series_the_log_cannot_prove():
    repo = _repo()
    _seed_two_rounds(repo)  # no jitter readings anywhere, no failures on gateway.local

    text = evidence_report_prometheus(build_evidence_report(_container(repo), days=7, until=UNTIL))

    assert _prom_samples(text, "netscope_check_jitter_ms") == []
    assert "# TYPE netscope_check_jitter_ms" not in text  # the family stays absent entirely

    # The bandwidth reading never measured latency: no per-check latency
    # series for it, though its loss reading (0.0 recorded) still speaks.
    assert not [line for line in _prom_samples(text, "netscope_check_latency_ms") if 'target="https://speed.example/down"' in line]
    assert [line for line in _prom_samples(text, "netscope_check_packet_loss_pct") if 'target="https://speed.example/down"' in line]


def test_prometheus_empty_log_is_report_zeros_and_nothing_else():
    text = evidence_report_prometheus(build_evidence_report(_container(_repo()), days=7, until=UNTIL))

    assert "netscope_report_measurements 0" in text
    assert "netscope_report_targets 0" in text
    assert "netscope_report_mean_score" not in text  # no scored rounds, no fabricated mean
    assert not _prom_samples(text, "netscope_target_measurements")


def test_prometheus_escapes_label_values_per_the_spec():
    repo = _repo()
    repo.save(_m(target='weird"host\\x', timestamp=UNTIL - timedelta(hours=1)))

    text = evidence_report_prometheus(build_evidence_report(_container(repo), days=7, until=UNTIL))

    assert 'netscope_target_measurements{target="weird\\"host\\\\x"} 1' in text
