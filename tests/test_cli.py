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
from datetime import UTC, datetime, timedelta

import pytest

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement, RouteHop, RouteSnapshot
from netscope.persistence.measurement_repository import MeasurementRepository
from netscope.ui.cli import (
    build_arg_parser,
    run_check_command,
    run_diagnose_command,
    run_history_command,
    run_route_command,
    run_service_add_command,
    run_service_check_command,
    run_service_list_command,
    run_service_remove_command,
    run_speed_command,
)


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


def test_parser_diagnose_defaults_gateway_to_auto():
    """v0.2: --gateway defaults to 'auto' (OS-detected default gateway,
    falling back to config/none) so gateway localization works out of
    the box -- see app/gateway.py's resolution policy."""
    args = build_arg_parser().parse_args(["diagnose"])
    assert args.gateway == "auto"


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
        probe_registry=ProbeRegistry(
            probes={ProbeType.ICMP: _FakeProbe(ProbeType.ICMP), ProbeType.DNS: _FakeProbe(ProbeType.DNS), ProbeType.HTTP: bad_http}
        )
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
        probe_registry=ProbeRegistry(
            probes={ProbeType.ICMP: _FakeProbe(ProbeType.ICMP), ProbeType.DNS: _FakeProbe(ProbeType.DNS), ProbeType.HTTP: bad_http}
        )
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
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
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


# ===========================================================================
# TASK-037 -- route command
# ===========================================================================


@dataclass
class _FakeTracerouteProbe:
    """Conforms to core.ports.Probe structurally, exactly like
    _FakeProbe above -- returns a scripted RawMeasurement carrying a
    RouteSnapshot in .extra["route"], mirroring
    probes/traceroute_probe.py's real, established convention."""

    hops: list = field(default_factory=list)
    success: bool = True
    error: str = "permission denied"
    calls: list = field(default_factory=list)
    probe_type: ProbeType = ProbeType.TRACEROUTE

    def run(self, target: str, **options) -> RawMeasurement:
        self.calls.append(target)
        if not self.success:
            return RawMeasurement(probe_type=ProbeType.TRACEROUTE, target=target, success=False, error=self.error)
        snapshot = RouteSnapshot(target=target, hops=self.hops)
        return RawMeasurement(probe_type=ProbeType.TRACEROUTE, target=target, success=True, extra={"route": snapshot})


def _sample_hops():
    return [
        RouteHop(ttl=1, address="10.0.0.1", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0),
        RouteHop(ttl=2, address="203.0.113.1", hostname=None, avg_rtt_ms=15.0, packet_loss_pct=0.0),
    ]


# --- A. Command registration --------------------------------------------------


def test_parser_route_subcommand_is_recognized():
    args = build_arg_parser().parse_args(["route", "example.com"])
    assert args.command == "route"


# --- B. Required target ------------------------------------------------------


def test_parser_route_without_a_target_is_rejected():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["route"])


# --- C. Target propagation (parser level) ------------------------------------


def test_parser_route_captures_the_exact_target_given():
    args = build_arg_parser().parse_args(["route", "example.com"])
    assert args.target == "example.com"


def test_parser_route_accepts_an_ip_address_target():
    args = build_arg_parser().parse_args(["route", "1.1.1.1"])
    assert args.target == "1.1.1.1"


def test_parser_route_accepts_verbose_after_target():
    args = build_arg_parser().parse_args(["route", "example.com", "-v"])
    assert args.verbose is True
    assert args.target == "example.com"


def test_parser_route_rejects_unknown_options():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["route", "example.com", "--totally-unknown-flag"])


# --- C/D. Target propagation + traceroute wiring (command level) ------------


def test_route_command_invokes_the_traceroute_probe_via_the_container(capsys):
    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "example.com")

    assert fake.calls == ["example.com"]


def test_route_command_propagates_the_exact_target(capsys):
    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "192.0.2.55")

    assert fake.calls == ["192.0.2.55"]


# --- E. RouteSnapshot handling ------------------------------------------------


def test_route_command_presents_every_hop_from_the_snapshot(capsys):
    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert "10.0.0.1" in captured.out
    assert "203.0.113.1" in captured.out


def test_route_command_presents_a_hop_with_no_response_without_fabricating_data(capsys):
    hops = [RouteHop(ttl=1, address=None, hostname=None, avg_rtt_ms=None, packet_loss_pct=100.0)]
    fake = _FakeTracerouteProbe(hops=hops)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert "*" in captured.out  # unknown address shown honestly, not fabricated
    assert "no response" in captured.out


# --- F. Route-analysis wiring --------------------------------------------------


def test_route_command_reports_a_first_observation_for_a_single_snapshot(capsys):
    """A single traceroute has nothing to compare against -- the CLI
    must present that honestly as a FIRST OBSERVATION, not claim a
    stability the evidence does not support (analyze_route_churn's
    documented behavior, v0.8's wording)."""
    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert "first observation" in captured.out.lower()


def test_route_command_reports_stability_across_stored_runs(capsys):
    """With route memory wired and an unchanged stored path, the verdict
    names the run count and the window -- real cross-run evidence, not
    the single-snapshot non-verdict."""
    from datetime import UTC, datetime

    class _RememberingRepo:
        def __init__(self) -> None:
            self.saved: list[RouteSnapshot] = []

        def save(self, snapshot: RouteSnapshot) -> None:
            self.saved.append(snapshot)

        def load_recent(self, target: str, limit: int = 20) -> list[RouteSnapshot]:
            return [
                RouteSnapshot(
                    target=target,
                    timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
                    hops=list(_sample_hops()),
                )
            ]

    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(
        probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}),
        route_repository=_RememberingRepo(),
    )

    run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert "unchanged across the last 2 runs" in captured.out
    assert "same path since" in captured.out


def _hop_like(address: str, *, ttl: int = 1) -> RouteHop:
    return RouteHop(ttl=ttl, address=address, hostname=None, avg_rtt_ms=10.0, packet_loss_pct=0.0)


def test_route_command_reports_the_changed_hops_when_the_path_moves(capsys):
    """A stored path that differs from this run's: the verdict leads
    with WHEN it changed and WHICH hop moved -- the v0.8 promise."""
    from datetime import UTC, datetime

    previous_hops = [_hop_like(addr, ttl=i + 1) for i, addr in enumerate(["10.0.0.1", "203.0.113.7"])]
    new_hops = [_hop_like(addr, ttl=i + 1) for i, addr in enumerate(["10.0.0.1", "198.51.100.9"])]

    class _ChangingRepo:
        def save(self, snapshot: RouteSnapshot) -> None: ...

        def load_recent(self, target: str, limit: int = 20) -> list[RouteSnapshot]:
            return [RouteSnapshot(target=target, timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC), hops=previous_hops)]

    fake = _FakeTracerouteProbe(hops=new_hops)
    container = Container(
        probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}),
        route_repository=_ChangingRepo(),
    )

    run_route_command(container, "example.com")

    captured = " ".join(capsys.readouterr().out.split())  # rich wraps long lines; collapse before asserting
    assert "changed vs 2026-11-09 10:00:00" in captured
    assert "hop 2: 203.0.113.7 → 198.51.100.9" in captured


def test_route_command_json_carries_the_full_stability_shape(capsys):
    """--json extends the stability block (prior_runs / window_start /
    last_change with the full hop diff) without touching the existing
    keys -- machine consumers keep working."""
    import json as _json
    from datetime import UTC, datetime

    class _RememberingRepo:
        def save(self, snapshot: RouteSnapshot) -> None: ...

        def load_recent(self, target: str, limit: int = 20) -> list[RouteSnapshot]:
            return [
                RouteSnapshot(
                    target=target,
                    timestamp=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
                    hops=[_hop_like("10.0.0.1", ttl=1), _hop_like("203.0.113.7", ttl=2)],
                )
            ]

    fake = _FakeTracerouteProbe(hops=[_hop_like("10.0.0.1", ttl=1), _hop_like("198.51.100.9", ttl=2)])
    container = Container(
        probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}),
        route_repository=_RememberingRepo(),
    )

    exit_code = run_route_command(container, "example.com", json_output=True)

    assert exit_code == 0
    payload = _json.loads(capsys.readouterr().out)
    stability = payload["stability"]
    assert stability["is_stable"] is False
    assert stability["prior_runs"] == 1
    assert stability["window_start"] == "2026-11-09T10:00:00+00:00"
    assert stability["last_change"]["changed_hops"] == [{"ttl": 2, "previous_address": "203.0.113.7", "new_address": "198.51.100.9"}]
    # pre-existing keys keep their shape
    assert stability["change_count"] == 1
    assert stability["snapshot_count"] == 2


def test_route_command_does_not_reimplement_route_churn_logic():
    """Static guard: the churn/stability decision must come from
    core.routing.analyze_route_churn (reached via the
    run_route_analysis use case since v0.2 -- the enrichment and
    analysis live in app/, never re-derived here from hop counts or
    addresses computed inline)."""
    import inspect

    source = inspect.getsource(run_route_command)
    assert "run_route_analysis(" in source
    assert "analyze_route_churn(" not in source  # it lives in the use case now, called exactly once there
    assert "signature()" not in source  # that's analyze_route_churn's own internal mechanism


# --- G. Human-readable output / H. no raw repr --------------------------------


def test_route_command_output_is_human_readable_not_a_raw_repr(capsys):
    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert "RouteSnapshot(" not in captured.out
    assert "RouteHop(" not in captured.out
    assert "RouteChurnResult(" not in captured.out


def test_route_command_output_includes_the_target_name(capsys):
    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert "example.com" in captured.out


# --- Failure handling ----------------------------------------------------------


def test_route_command_presents_a_failed_traceroute_cleanly(capsys):
    fake = _FakeTracerouteProbe(success=False, error="permission denied: run as root/Administrator")
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    exit_code = run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "permission denied" in captured.out.lower()
    assert "RouteSnapshot(" not in captured.out


def test_route_command_does_not_fabricate_a_route_on_failure(capsys):
    fake = _FakeTracerouteProbe(success=False)
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    run_route_command(container, "example.com")

    captured = capsys.readouterr()
    assert "stable" not in captured.out.lower()
    assert "hop" not in captured.out.lower()


def test_route_command_returns_zero_on_successful_completion(capsys):
    fake = _FakeTracerouteProbe(hops=_sample_hops())
    container = Container(probe_registry=ProbeRegistry(probes={ProbeType.TRACEROUTE: fake}))

    exit_code = run_route_command(container, "example.com")

    assert exit_code == 0


# --- I. Existing commands remain intact ---------------------------------------


def test_diagnose_subcommand_still_works_after_adding_route():
    args = build_arg_parser().parse_args(["diagnose", "--gateway", "10.0.0.1"])
    assert args.command == "diagnose"
    assert args.gateway == "10.0.0.1"


def test_bare_invocation_still_has_no_subcommand_after_adding_route():
    args = build_arg_parser().parse_args([])
    assert args.command is None


def test_diagnose_command_function_still_callable_after_route_addition(capsys):
    """Regression guard: adding route wiring must not have disturbed
    run_diagnose_command's own signature/behavior."""
    probes = {
        ProbeType.ICMP: _FakeProbe(ProbeType.ICMP),
        ProbeType.DNS: _FakeProbe(ProbeType.DNS),
        ProbeType.HTTP: _FakeProbe(ProbeType.HTTP),
    }
    container = Container(probe_registry=ProbeRegistry(probes=probes))
    config = NetScopeConfig()

    exit_code = run_diagnose_command(container, config)

    assert exit_code == 0


# --- J. Dependency boundary (route-specific) -----------------------------------


def test_cli_module_still_does_not_import_adapters_after_route_addition():
    """route wiring reaches TracerouteProbeAdapter only through
    container.probe_registry -- cli.py must never import
    netscope.adapters.probes.traceroute_adapter or icmplib directly."""
    import netscope.ui.cli as module

    full_paths = _imports_of(module)
    assert not any(p.startswith("netscope.adapters") for p in full_paths)
    assert "icmplib" not in full_paths
    assert "subprocess" not in full_paths


def test_route_command_does_not_import_probe_internals_directly():
    """Checks actual code only (AST, docstring stripped) -- the
    function's own docstring legitimately explains this decision by
    naming icmplib as what it deliberately avoids."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(run_route_command))
    func_body = tree.body[0].body[1:]  # skip the docstring Expr node
    code_only = ast.unparse(ast.Module(body=func_body, type_ignores=[]))
    assert "icmplib" not in code_only
    assert "TracerouteProbeAdapter(" not in code_only
    assert "subprocess" not in code_only


# ---------------------------------------------------------------------------
# speed (v0.3)
# ---------------------------------------------------------------------------


def test_parser_speed_subcommand_is_recognized():
    args = build_arg_parser().parse_args(["speed"])
    assert args.command == "speed"
    assert args.url is None
    assert args.seconds is None
    assert args.json is False


def test_parser_speed_accepts_url_and_seconds_overrides():
    args = build_arg_parser().parse_args(["speed", "--url", "https://x.example/f", "--seconds", "3.5"])
    assert args.url == "https://x.example/f"
    assert args.seconds == 3.5


def _speed_container(measurement: RawMeasurement):
    """A Container whose registry serves one scripted BANDWIDTH probe,
    recording how it was invoked."""
    calls = []

    class _ScriptedBandwidthProbe:
        probe_type = ProbeType.BANDWIDTH

        def run(self, target, **options):
            calls.append({"target": target, **options})
            return measurement

    probes = {ProbeType.BANDWIDTH: _ScriptedBandwidthProbe()}
    container = Container(probe_registry=ProbeRegistry(probes=probes))
    return container, calls


def test_speed_command_runs_through_the_registry_and_reports_json(capsys):
    measurement = RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target="https://speed.example/down",
        success=True,
        extra={
            "throughput_mbps": 42.3,
            "peak_mbps": 51.0,
            "bytes_transferred": 31_500_000,
            "duration_s": 6.0,
            "sample_count": 11,
        },
    )
    container, calls = _speed_container(measurement)

    exit_code = run_speed_command(container, NetScopeConfig(), json_output=True)

    assert exit_code == 0
    assert calls == [{"target": NetScopeConfig().bandwidth_url, "duration_s": NetScopeConfig().bandwidth_seconds}]
    out = capsys.readouterr().out
    assert '"throughput_mbps"' in out
    assert "42.3" in out


def test_speed_command_forwards_flag_overrides_and_fails_on_failure(capsys):
    measurement = RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target="https://x.example/f",
        success=False,
        error="HTTP 404",
        error_type=ProbeErrorType.HTTP_FAILURE,
    )
    container, calls = _speed_container(measurement)

    exit_code = run_speed_command(
        container,
        NetScopeConfig(),
        url="https://x.example/f",
        seconds=2.0,
        json_output=True,
    )

    assert exit_code == 1
    assert calls == [{"target": "https://x.example/f", "duration_s": 2.0}]


def test_speed_command_text_output_mentions_the_number(capsys):
    measurement = RawMeasurement(
        probe_type=ProbeType.BANDWIDTH,
        target="https://speed.example/down",
        success=True,
        extra={"throughput_mbps": 88.4, "peak_mbps": 91.2, "bytes_transferred": 66_300_000, "duration_s": 6.0, "sample_count": 12},
    )
    container, _ = _speed_container(measurement)

    exit_code = run_speed_command(container, NetScopeConfig())

    assert exit_code == 0
    assert "88.4" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# service (v0.7) -- argument parsing + command wiring
# ---------------------------------------------------------------------------


def test_parser_service_add_takes_name_host_and_checks():
    args = build_arg_parser().parse_args(["service", "add", "My VPN", "vpn.example.net", "--checks", "icmp,http"])
    assert args.command == "service"
    assert args.service_command == "add"
    assert args.name == "My VPN"
    assert args.host == "vpn.example.net"
    assert args.checks == "icmp,http"


def test_parser_service_add_defaults_checks_to_all_five():
    args = build_arg_parser().parse_args(["service", "add", "My VPN", "vpn.example.net"])
    assert args.checks == "icmp,dns,tcp,tls,http"


def test_parser_service_list_remove_check_shapes():
    listed = build_arg_parser().parse_args(["service", "list", "--json"])
    assert (listed.service_command, listed.json) == ("list", True)
    removed = build_arg_parser().parse_args(["service", "remove", "My VPN"])
    assert (removed.service_command, removed.name) == ("remove", "My VPN")
    checked = build_arg_parser().parse_args(["service", "check", "My VPN"])
    assert (checked.service_command, checked.name, checked.json) == ("check", "My VPN", False)


def test_parser_service_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["service"])


def _service_container(tmp_path):
    """A container wired to a real (temp-file) ServiceRepository and
    fake probes -- the service commands persist for real, exactly as
    run_check_command's tests persist measurements for real."""
    from netscope.persistence.measurement_repository import MeasurementRepository
    from netscope.persistence.service_repository import ServiceRepository

    container, probes = _fake_container(measurement_repository=MeasurementRepository.open(":memory:"))
    container.service_repository = ServiceRepository.open(tmp_path / "services.db")
    return container, probes


def test_service_add_saves_and_reports_created(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    code = run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.ICMP, ProbeType.HTTP})
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved service" in out and "My VPN" in out and "vpn.example.net" in out
    assert "icmp, http" in out  # CLI display order, not alphabetical storage order


def test_service_add_twice_reports_updated(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    run_service_add_command(container, "My VPN", "old.example.net", {ProbeType.ICMP})
    code = run_service_add_command(container, "My VPN", "new.example.net", {ProbeType.TLS})
    out = capsys.readouterr().out
    assert code == 0
    assert "Updated service" in out and "new.example.net" in out
    assert container.service_repository.load_all()[0].host == "new.example.net"


def test_service_add_rejects_an_empty_host(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    code = run_service_add_command(container, "My VPN", "  ", {ProbeType.ICMP})
    out = capsys.readouterr().out
    assert code == 1
    assert "host" in out
    assert container.service_repository.load_all() == []


def test_service_list_empty_then_populated(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    assert run_service_list_command(container) == 0
    out = capsys.readouterr().out
    assert "No saved services yet" in out

    run_service_add_command(container, "alpha cdn", "a.example.net", {ProbeType.HTTP})
    run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.ICMP})
    assert run_service_list_command(container) == 0
    out = capsys.readouterr().out
    assert "alpha cdn" in out and "My VPN" in out
    # alphabetical, not insertion order
    assert out.index("alpha cdn") < out.index("My VPN")


def test_service_list_json_payload(tmp_path, capsys):
    import json as jsonlib

    container, _probes = _service_container(tmp_path)
    run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.HTTP, ProbeType.ICMP})
    capsys.readouterr()  # drain the add confirmation; only the JSON should follow
    assert run_service_list_command(container, json_output=True) == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload["report"] == "services"
    assert payload["services"] == [{"name": "My VPN", "host": "vpn.example.net", "checks": ["icmp", "http"]}]


def test_service_remove_reports_removed_and_no_such_service(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    assert run_service_remove_command(container, "My VPN") == 1  # nothing saved yet
    out = capsys.readouterr().out
    assert "no saved service named 'My VPN'" in out

    run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.ICMP})
    assert run_service_remove_command(container, "My VPN") == 0
    out = capsys.readouterr().out
    assert "Removed service" in out


def test_service_remove_typo_names_what_is_saved(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.ICMP})
    assert run_service_remove_command(container, "My VPM") == 1
    out = capsys.readouterr().out
    assert "no saved service named 'My VPM'" in out and "My VPN" in out


def test_service_check_runs_the_curated_checks_and_persists(tmp_path, capsys):
    container, probes = _service_container(tmp_path)
    run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.ICMP})

    code = run_service_check_command(container, "My VPN")
    out = capsys.readouterr().out
    assert code == 0
    assert "My VPN — checks against vpn.example.net" in out
    assert probes[ProbeType.ICMP].calls == ["vpn.example.net"]
    # persisted exactly like netscope check does
    assert container.measurement_repository.recent("vpn.example.net", limit=5)


def test_service_check_unknown_name_lists_what_is_saved(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.ICMP})
    code = run_service_check_command(container, "typo")
    out = capsys.readouterr().out
    assert code == 1
    assert "no saved service named 'typo'" in out and "My VPN" in out


def test_service_check_on_an_empty_list_says_so(tmp_path, capsys):
    container, _probes = _service_container(tmp_path)
    code = run_service_check_command(container, "My VPN")
    out = capsys.readouterr().out
    assert code == 1
    assert "the saved list is empty" in out


def test_service_check_json_payload(tmp_path, capsys):
    import json as jsonlib

    container, _probes = _service_container(tmp_path)
    run_service_add_command(container, "My VPN", "vpn.example.net", {ProbeType.ICMP})
    capsys.readouterr()  # drain the add confirmation; only the JSON should follow
    code = run_service_check_command(container, "My VPN", json_output=True)
    payload = jsonlib.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["report"] == "service-check"
    assert payload["service"] == "My VPN"
    assert payload["target"] == "vpn.example.net"


def test_netscope_check_still_renders_through_the_shared_presentation(tmp_path, capsys):
    """The v0.7 refactor routes run_check_command through the same
    _print_check_result helper as service check -- the legacy output
    shape is preserved exactly (same title, same score line)."""
    container, _probes = _fake_container()
    code = run_check_command(container, "1.1.1.1", {ProbeType.ICMP})
    out = capsys.readouterr().out
    assert code == 0
    assert "Checks against 1.1.1.1" in out
    assert "Experience score:" in out


# ---------------------------------------------------------------------------
# History score trend (v0.10): the rounds table gains a one-line
# sparkline of the score series -- no chart dependency, printed only
# when there is an actual trend to show.
# ---------------------------------------------------------------------------


def test_history_command_prints_the_score_trend_line(capsys):
    repo = MeasurementRepository.open(":memory:")
    base = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    for round_index in range(3):
        for check_index, probe_type in enumerate((ProbeType.ICMP, ProbeType.DNS, ProbeType.HTTP)):
            repo.save(
                RawMeasurement(
                    probe_type=probe_type,
                    target=f"t{probe_type.value}",
                    timestamp=base + timedelta(seconds=round_index * 180 + check_index),
                    success=True,
                    latency_ms=10.0 + round_index * 5,
                )
            )
    container, _ = _fake_container(measurement_repository=repo)

    exit_code = run_history_command(container, limit=20)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "score trend" in out
    assert "over 3 scored rounds" in out
    repo.close()


def test_history_command_stays_quiet_without_a_trend(capsys):
    repo = MeasurementRepository.open(":memory:")
    container, _ = _fake_container(measurement_repository=repo)
    exit_code = run_history_command(container, limit=20)
    assert exit_code == 0
    assert "score trend" not in capsys.readouterr().out
    repo.close()
