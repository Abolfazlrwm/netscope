"""
Tests for netscope.ui.cli (TASK-035).

Per architecture-overview.md SS3's `ui` test-strategy row: "Thin enough
that it mostly doesn't need dedicated tests beyond argument-parsing
edge cases." These tests only exercise build_arg_parser() -- never
main() itself, which would build a real Container (real probes, real
network) and a real MeasurementRepository (real filesystem). That's
intentional: the actual orchestration this parser feeds into is
app.use_cases.run_measurement_round, already covered offline in
tests/test_use_cases.py.
"""

from __future__ import annotations

import ast

import pytest

from netscope.ui.cli import build_arg_parser


def test_parser_with_no_arguments_defaults_gateway_to_none():
    args = build_arg_parser().parse_args([])
    assert args.gateway is None


def test_parser_with_no_arguments_defaults_verbose_to_false():
    args = build_arg_parser().parse_args([])
    assert args.verbose is False


def test_parser_accepts_gateway_flag():
    args = build_arg_parser().parse_args(["--gateway", "192.168.1.1"])
    assert args.gateway == "192.168.1.1"


def test_parser_accepts_verbose_long_flag():
    args = build_arg_parser().parse_args(["--verbose"])
    assert args.verbose is True


def test_parser_accepts_verbose_short_flag():
    args = build_arg_parser().parse_args(["-v"])
    assert args.verbose is True


def test_parser_accepts_both_gateway_and_verbose_together():
    args = build_arg_parser().parse_args(["--gateway", "10.0.0.1", "-v"])
    assert args.gateway == "10.0.0.1"
    assert args.verbose is True


def test_parser_rejects_an_unknown_argument():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--totally-unknown-flag"])


def test_parser_gateway_requires_a_value():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--gateway"])


# ---------------------------------------------------------------------------
# ui dependency boundary
# ---------------------------------------------------------------------------


def test_cli_module_does_not_import_adapters_or_sqlite3_directly():
    """architecture-overview.md SS3: ui may depend on app/core only --
    never adapters or persistence directly (this module's one narrow,
    documented exception is MeasurementRepository -- allowed to import
    the persistence *entry point*, but never adapters or sqlite3
    itself)."""
    import netscope.ui.cli as module

    tree = ast.parse(open(module.__file__).read())
    full_paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            full_paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            full_paths.add(node.module)

    assert "sqlite3" not in full_paths
    assert not any(p.startswith("netscope.adapters") for p in full_paths)


def test_cli_module_does_not_contain_probe_orchestration_logic():
    """No direct probe-running/scoring/diagnosis logic should exist in
    ui/cli.py after the TASK-035 rebuild -- it must call exactly one
    app use case (run_measurement_round) for that."""
    import inspect

    import netscope.ui.cli as module

    source = inspect.getsource(module)
    assert "score_measurements(" not in source
    assert "evidence_from_latency(" not in source
    assert "diagnose(" not in source
    assert "ProbeRegistry(" not in source
