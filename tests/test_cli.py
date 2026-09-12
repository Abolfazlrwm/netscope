"""
Tests for netscope.ui.cli (TASK-035/036).

Per architecture-overview.md SS3's `ui` test-strategy row: "Thin enough
that it mostly doesn't need dedicated tests beyond argument-parsing
edge cases," and future-roadmap.md's TASK-036 row: "Unit tests for
command wiring with fake app use case."

Argument-parsing tests exercise build_arg_parser() directly. Command-
wiring tests exercise run_diagnose_command() with a fake Container
(fake ProbeRegistry, in-memory MeasurementRepository) -- never main()
itself, which would build a real Container hitting real probes/network.

TASK-036 changed `--gateway` from a top-level flag to a `diagnose`
subcommand option (`netscope diagnose --gateway ...` instead of
`netscope --gateway ...`) -- this is the roadmap's own explicit
architectural direction (introducing `netscope diagnose` as a named
subcommand), not an accidental regression, so the TASK-035 tests
asserting the old flat syntax are updated here rather than preserved
verbatim. Bare `netscope` (no subcommand at all) still defaults to the
same diagnose behavior, which the "no subcommand" tests below confirm.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

import pytest

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.core.models import ProbeType, RawMeasurement
from netscope.persistence.measurement_repository import MeasurementRepository
from netscope.ui.cli import build_arg_parser, run_diagnose_command


@dataclass
class _FakeProbe:
    probe_type: ProbeType
    latency_ms: float = 10.0
    success: bool = True
    calls: list = field(default_factory=list)

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        return RawMeasurement(probe_type=self.probe_type, target=target, success=self.success, latency_ms=self.latency_ms)


def _fake_container(measurement_repository=None):
    probes = {
        ProbeType.ICMP: _FakeProbe(ProbeType.ICMP),
        ProbeType.DNS: _FakeProbe(ProbeType.DNS),
        ProbeType.HTTP: _FakeProbe(ProbeType.HTTP),
    }
    container = Container(probe_registry=ProbeRegistry(probes=probes), measurement_repository=measurement_repository)
    return container, probes


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parser_with_no_subcommand_has_command_none():
    args = build_arg_parser().parse_args([])
    assert args.command is None


def test_parser_with_no_arguments_defaults_verbose_to_false():
    args = build_arg_parser().parse_args([])
    assert args.verbose is False


def test_parser_bare_invocation_has_a_verbose_attribute_at_all():
    """A bare invocation (no subcommand) never touches the diagnose
    subparser's own --verbose definition -- set_defaults on the
    top-level parser ensures args.verbose still exists safely."""
    args = build_arg_parser().parse_args([])
    assert hasattr(args, "verbose")


def test_parser_diagnose_subcommand_is_recognized():
    args = build_arg_parser().parse_args(["diagnose"])
    assert args.command == "diagnose"


def test_parser_diagnose_defaults_gateway_to_none():
    args = build_arg_parser().parse_args(["diagnose"])
    assert args.gateway is None


def test_parser_diagnose_accepts_gateway_flag():
    args = build_arg_parser().parse_args(["diagnose", "--gateway", "192.168.1.1"])
    assert args.gateway == "192.168.1.1"


def test_parser_verbose_flag_belongs_to_the_subcommand_not_the_top_level():
    """--verbose is attached only to each subcommand's own parser (see
    build_arg_parser's docstring for why: attaching it to both the
    top-level parser and a subparser via `parents=` triggers a
    documented argparse quirk where the subparser's default silently
    overwrites an already-parsed top-level value). `netscope diagnose
    -v` is the supported form; a bare top-level `--verbose` is not."""
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--verbose", "diagnose"])


def test_parser_accepts_verbose_short_flag_after_subcommand():
    args = build_arg_parser().parse_args(["diagnose", "-v"])
    assert args.verbose is True


def test_parser_diagnose_accepts_both_gateway_and_verbose_together():
    args = build_arg_parser().parse_args(["diagnose", "--gateway", "10.0.0.1", "-v"])
    assert args.gateway == "10.0.0.1"
    assert args.verbose is True


def test_parser_rejects_an_unknown_top_level_argument():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--totally-unknown-flag"])


def test_parser_rejects_an_unknown_subcommand():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["not-a-real-command"])


def test_parser_diagnose_gateway_requires_a_value():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["diagnose", "--gateway"])


def test_parser_gateway_is_not_recognized_without_the_diagnose_subcommand():
    """Confirms the TASK-036 syntax change is real and intentional:
    --gateway now belongs to `diagnose`, not the top-level parser."""
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--gateway", "10.0.0.1"])


# ---------------------------------------------------------------------------
# Command wiring (fake app use case / fake Container)
# ---------------------------------------------------------------------------


def test_diagnose_command_invokes_the_probe_registry_via_the_container(capsys):
    container, probes = _fake_container()
    config = NetScopeConfig()

    run_diagnose_command(container, config)

    assert probes[ProbeType.ICMP].calls == [config.public_dns_target]  # no gateway given
    assert probes[ProbeType.DNS].calls == [config.dns_lookup_domain]
    assert probes[ProbeType.HTTP].calls == [config.public_cdn_url]


def test_diagnose_command_propagates_the_gateway_argument(capsys):
    container, probes = _fake_container()
    config = NetScopeConfig()

    run_diagnose_command(container, config, gateway="192.168.1.1")

    assert probes[ProbeType.ICMP].calls[0] == "192.168.1.1"


def test_diagnose_command_propagates_configured_targets(capsys):
    container, probes = _fake_container()
    config = NetScopeConfig(public_dns_target="9.9.9.9", dns_lookup_domain="custom.example", public_cdn_url="https://cdn.example/")

    run_diagnose_command(container, config)

    assert probes[ProbeType.ICMP].calls == ["9.9.9.9"]
    assert probes[ProbeType.DNS].calls == ["custom.example"]
    assert probes[ProbeType.HTTP].calls == ["https://cdn.example/"]


def test_diagnose_command_returns_zero_on_ordinary_completion(capsys):
    container, _ = _fake_container()
    config = NetScopeConfig()

    exit_code = run_diagnose_command(container, config)

    assert exit_code == 0


def test_diagnose_command_returns_zero_even_when_a_problem_is_diagnosed(capsys):
    """A completed diagnosis, whether healthy or not, is not a CLI
    failure -- no exit-code taxonomy for diagnosis outcomes."""
    bad_http = _FakeProbe(ProbeType.HTTP, latency_ms=900.0)
    container = Container(
        probe_registry=ProbeRegistry(probes={ProbeType.ICMP: _FakeProbe(ProbeType.ICMP), ProbeType.DNS: _FakeProbe(ProbeType.DNS), ProbeType.HTTP: bad_http})
    )
    config = NetScopeConfig()

    exit_code = run_diagnose_command(container, config, gateway="10.0.0.1")

    assert exit_code == 0


# --- Output / presentation --------------------------------------------------


def test_diagnose_command_prints_the_experience_score(capsys):
    container, _ = _fake_container()
    config = NetScopeConfig()

    run_diagnose_command(container, config)

    captured = capsys.readouterr()
    assert "Experience score:" in captured.out


def test_diagnose_command_prints_a_diagnostic_message_without_a_gateway(capsys):
    container, _ = _fake_container()
    config = NetScopeConfig()

    run_diagnose_command(container, config)  # no gateway -> INSUFFICIENT_EVIDENCE, not "healthy"

    captured = capsys.readouterr()
    # Without a gateway this is INSUFFICIENT_EVIDENCE (untested-gateway
    # principle, TASK-026), not a clean "No issues detected." -- that
    # message is exercised by the explicit-gateway/mature-baseline case
    # in tests/test_use_cases.py's run_measurement_round coverage. This
    # test instead confirms *some* diagnostic explanation is printed,
    # never a raw Diagnosis(...) repr.
    assert "Diagnosis(" not in captured.out
    assert "Not enough evidence" in captured.out or "No issues detected." in captured.out


def test_diagnose_command_never_prints_a_raw_diagnosis_repr(capsys):
    bad_http = _FakeProbe(ProbeType.HTTP, latency_ms=900.0)
    container = Container(
        probe_registry=ProbeRegistry(probes={ProbeType.ICMP: _FakeProbe(ProbeType.ICMP), ProbeType.DNS: _FakeProbe(ProbeType.DNS), ProbeType.HTTP: bad_http})
    )
    config = NetScopeConfig()

    run_diagnose_command(container, config, gateway="10.0.0.1")

    captured = capsys.readouterr()
    assert "Diagnosis(" not in captured.out
    assert "Evidence(" not in captured.out


# --- Persistence wiring -------------------------------------------------------


def test_diagnose_command_saves_measurements_when_container_has_a_repository(capsys):
    repo = MeasurementRepository.open(":memory:")
    container, _ = _fake_container(measurement_repository=repo)
    config = NetScopeConfig()

    run_diagnose_command(container, config)

    saved = repo.recent(config.public_dns_target)
    assert len(saved) == 1
    repo.close()


def test_diagnose_command_does_not_require_a_repository(capsys):
    """container.measurement_repository defaults to None (TASK-035) --
    the diagnose command must still work without one."""
    container, _ = _fake_container(measurement_repository=None)
    config = NetScopeConfig()

    exit_code = run_diagnose_command(container, config)  # must not raise

    assert exit_code == 0


# ---------------------------------------------------------------------------
# ui dependency boundary
# ---------------------------------------------------------------------------


def _imports_of(module) -> set[str]:
    tree = ast.parse(open(module.__file__).read())
    full_paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            full_paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            full_paths.add(node.module)
    return full_paths


def test_cli_module_does_not_import_persistence_adapters_or_sqlite3_directly():
    """TASK-036 removed even the narrow MeasurementRepository import
    TASK-035 had -- persistence is now reached only via
    container.measurement_repository, wired by the composition root."""
    import netscope.ui.cli as module

    full_paths = _imports_of(module)
    assert "sqlite3" not in full_paths
    assert not any(p.startswith("netscope.adapters") for p in full_paths)
    assert not any(p.startswith("netscope.persistence") for p in full_paths)


def test_cli_module_does_not_import_low_level_network_libraries():
    import netscope.ui.cli as module

    full_paths = _imports_of(module)
    for forbidden in ("socket", "ssl", "httpx", "dns", "icmplib"):
        assert forbidden not in full_paths


def test_cli_module_does_not_contain_probe_orchestration_or_diagnosis_logic():
    """No direct probe-running/scoring/diagnosis/evidence-construction
    logic should exist in ui/cli.py -- it must call exactly the existing
    run_measurement_round use case for that."""
    import inspect

    import netscope.ui.cli as module

    source = inspect.getsource(module)
    assert "score_measurements(" not in source
    assert "evidence_from_latency(" not in source
    assert " diagnose(" not in source and "= diagnose(" not in source
    assert "ProbeRegistry(" not in source
    assert "diagnose_service_with_connectivity(" not in source  # TASK-034 logic must not be duplicated here


def test_cli_module_does_not_define_a_redundant_diagnosis_use_case():
    """Guards against exactly the API proliferation this task's own
    instructions warn about (diagnose_now/run_diagnosis_cli/etc.) --
    the CLI must wire to the existing run_measurement_round, not a new
    duplicate."""
    import netscope.ui.cli as module

    forbidden_names = {"diagnose_now", "run_diagnosis_cli", "cli_diagnose", "perform_diagnostic", "analyze_target"}
    defined_names = {name for name in dir(module) if not name.startswith("_")}
    assert forbidden_names.isdisjoint(defined_names)
