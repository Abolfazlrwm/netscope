"""
netscope.ui.cli

NetScope's command-line interface -- a thin presentation layer over the
`app` use cases. Every command follows the same three rules the
architecture has enforced since the first CLI:

1. What the numbers MEAN (scoring, evidence, diagnosis, churn) is
   decided in `app`/`core`, never here.
2. Concrete dependencies (probes, persistence, lookups) arrive via the
   `Container` built by the composition root -- this module never
   imports `netscope.adapters` or `netscope.persistence` directly.
3. This layer only decides how results LOOK: rich panels/tables for
   humans (`ui/render.py`), pure JSON for machines (`app/reports.py`,
   the `--json` flag on every read command).

COMMANDS (v0.2)
---------------
- `netscope diagnose [--gateway auto|IP|none] [--json]`
    One measurement round (gateway / public DNS / DNS lookup / CDN),
    experience score, evidence-based diagnosis. `--gateway auto` (the
    default) detects the default gateway from the OS when no explicit
    address is configured.
- `netscope check <host> [--checks icmp,dns,tcp,tls,http] [--json]`
    Probe one arbitrary target with any subset of the five service
    checks, score and diagnose it.
- `netscope route <target> [--no-resolve] [--json]`
    Traceroute with reverse-DNS hostnames and (when an offline ASN
    database exists at ~/.netscope/ipasn.dat) AS numbers per hop.
- `netscope history [--limit N] [--json]`
    Recent measurement rounds reconstructed from the local log.
- `netscope monitor [--interval S] [--rounds N] [--gateway ...]`
    Continuous rounds with evidence-based incident detection: an
    incident only OPENS after a diagnosis is sustained for 3+ rounds
    (`core.incidents`), and only CLOSES after a sustained-healthy
    stretch -- single noisy samples never flap.
- `netscope tui`
    The live terminal dashboard (requires the `tui` extra:
    `pip install netscope[tui]`).
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable

from netscope.app.config import NetScopeConfig, load_config
from netscope.app.container import Container, build_container, configure_logging
from netscope.app.gateway import AUTO_SENTINEL, resolve_gateway
from netscope.app.monitoring import MonitorSession, TransitionType
from netscope.app.reports import (
    build_diagnose_report,
    serialize_experience,
    serialize_measurement,
    serialize_route_hops,
)
from netscope.app.use_cases import check_target, recent_rounds, run_measurement_round, run_route_analysis
from netscope.core.models import ProbeType
from netscope.explanation.explainer import explain
from netscope.ui.render import (
    console,
    diagnosis_panel,
    format_time,
    measurements_table,
    route_table,
    truncate,
)

# The five per-service check types, in a stable CLI-friendly order.
_CHECK_CHOICES = ("icmp", "dns", "tcp", "tls", "http")


def build_arg_parser() -> argparse.ArgumentParser:
    """Builds the CLI's argument parser -- extracted from main() so
    argument-parsing behavior is testable without executing a real
    measurement round.

    `--verbose`/`-v` is defined on a shared, help-suppressed parent
    parser and attached to every subcommand's own parser via
    `parents=` -- not to the top-level parser too, since argparse's
    `parents=` mechanism re-applies a shared option's *default* when
    the subparser runs, silently overwriting a value already parsed at
    the top level. `--verbose` is given after the subcommand name
    (`netscope diagnose -v`).
    """
    global_options = argparse.ArgumentParser(add_help=False)
    global_options.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable verbose (INFO-level) logging",
    )

    parser = argparse.ArgumentParser(
        prog="netscope",
        description="NetScope — from raw network measurements to 'what's wrong, and where', with evidence.",
    )
    parser.set_defaults(verbose=False)
    subparsers = parser.add_subparsers(dest="command")

    diagnose = subparsers.add_parser(
        "diagnose",
        help="run a diagnostic round (gateway/DNS/CDN checks) and report the result",
        parents=[global_options],
    )
    diagnose.add_argument(
        "--gateway",
        default=AUTO_SENTINEL,
        help="gateway to probe: an IP, 'auto' (detect from the OS, default), or 'none' (skip the gateway check)",
    )
    diagnose.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    check = subparsers.add_parser(
        "check",
        help="probe one target with any subset of the service checks and report the result",
        parents=[global_options],
    )
    check.add_argument("target", help="hostname or IP address to check")
    check.add_argument(
        "--checks",
        default=",".join(_CHECK_CHOICES),
        help="comma-separated subset of checks to run (default: all five)",
    )
    check.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    route = subparsers.add_parser(
        "route",
        help="trace the route to a target and report route stability",
        parents=[global_options],
    )
    route.add_argument("target", help="hostname or IP address to trace the route to")
    route.add_argument("--no-resolve", action="store_true", help="skip reverse-DNS hostname resolution for hops")
    route.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    history = subparsers.add_parser(
        "history",
        help="show recent measurement rounds from the local log",
        parents=[global_options],
    )
    history.add_argument("--limit", type=int, default=20, help="maximum number of rounds to show (default: 20)")
    history.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    monitor = subparsers.add_parser(
        "monitor",
        help="run continuous measurement rounds with incident detection (Ctrl+C to stop)",
        parents=[global_options],
    )
    monitor.add_argument("--interval", type=float, default=30.0, help="seconds between rounds (default: 30)")
    monitor.add_argument("--rounds", type=int, default=0, help="number of rounds before stopping (0 = until Ctrl+C)")
    monitor.add_argument("--gateway", default=AUTO_SENTINEL, help="gateway to probe: an IP, 'auto', or 'none'")

    subparsers.add_parser(
        "tui",
        help="launch the live terminal dashboard (requires: pip install netscope[tui])",
        parents=[global_options],
    )

    return parser


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------


def run_diagnose_command(
    container: Container,
    config: NetScopeConfig,
    gateway: str | None = None,
    *,
    json_output: bool = False,
) -> int:
    """Runs the `diagnose` command: one measurement round via
    `run_measurement_round` (the canonical diagnosis use case),
    persists the resulting measurements if the container was built with
    a repository, and prints a human-readable (or `--json`) result.

    Gateway handling: an explicit `gateway` argument wins; `None` (the
    function-level default, preserving the pre-0.2 contract for direct
    callers) means "no gateway measurement"; the CLI passes the parsed
    `--gateway` value, whose default `auto` resolves through
    `app.gateway.resolve_gateway` (explicit config -> OS detection ->
    none).

    Returns a process exit code. Always `0` on ordinary completion --
    a completed diagnosis, whether it found a problem or not, is not a
    CLI-level failure.
    """
    resolved_gateway = resolve_gateway(gateway, config) if gateway is not None else config.gateway
    measurements, experience, diagnosis = run_measurement_round(container, config, gateway=resolved_gateway)

    if container.measurement_repository is not None:
        for measurement in measurements:
            container.measurement_repository.save(measurement)

    if json_output:
        report = build_diagnose_report(measurements, experience, diagnosis, gateway=resolved_gateway)
        console().print_json(json.dumps(report))
        return 0

    c = console()
    c.print()
    c.print(f"Experience score: {experience.score:.0f}/100 ({experience.level.value})")
    c.print(measurements_table(measurements))
    c.print()
    c.print(diagnosis_panel(diagnosis))
    if resolved_gateway is None:
        c.print(
            "[dim]Tip: no gateway probed -- pass --gateway auto (or set gateway in ~/.netscope/config.toml) "
            "to localize Wi-Fi/router vs. ISP issues.[/dim]"
        )
    c.print()
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def parse_checks(spec: str) -> set[ProbeType]:
    """Parse a comma-separated check list ('icmp,dns') into ProbeTypes.
    Raises argparse-friendly ValueError on an unknown name."""
    names = [name.strip().lower() for name in spec.split(",") if name.strip()]
    if not names:
        raise ValueError("at least one check must be requested")
    by_value: dict[str, ProbeType] = {p.value: p for p in ProbeType if p.value in _CHECK_CHOICES}
    unknown = [n for n in names if n not in by_value]
    if unknown:
        raise ValueError(f"unknown check(s): {', '.join(unknown)}. Valid checks: {', '.join(_CHECK_CHOICES)}")
    return {by_value[n] for n in names}


def run_check_command(
    container: Container,
    target: str,
    checks: set[ProbeType] | frozenset[ProbeType],
    *,
    json_output: bool = False,
) -> int:
    """Runs the `check` command: the target's checks via `check_target`,
    plus score and diagnosis -- the same pipeline a Service goes
    through, on any target the user names."""
    measurements, experience, diagnosis = check_target(target, container, checks)

    if container.measurement_repository is not None:
        for measurement in measurements:
            container.measurement_repository.save(measurement)

    if json_output:
        report = build_diagnose_report(measurements, experience, diagnosis, gateway=None)
        report["report"] = "check"
        report["target"] = target
        console().print_json(json.dumps(report))
        return 0

    c = console()
    c.print()
    c.print(f"Experience score: {experience.score:.0f}/100 ({experience.level.value})")
    c.print(measurements_table(measurements, title=f"Checks against {target}"))
    c.print()
    c.print(diagnosis_panel(diagnosis))
    c.print()
    return 0


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


def run_route_command(
    container: Container,
    target: str,
    *,
    resolve_hostnames: bool = True,
    json_output: bool = False,
) -> int:
    """Runs the `route` command: one traceroute + enrichment + churn
    analysis via the `run_route_analysis` use case (never icmplib or
    core.routing re-implemented here), then presentation.

    A failed traceroute (e.g. permission denied, no hops obtained) is
    presented as a clean failure message, never a fabricated empty or
    successful route -- and returns exit code `1`, distinct from the
    diagnose commands' always-`0`: a diagnosis finding a problem still
    completed successfully, but a traceroute that could not produce any
    route data did not complete its job.
    """
    result = run_route_analysis(target, container, resolve_hostnames=resolve_hostnames)

    if json_output:
        payload = {
            "tool": "netscope",
            "report": "route",
            "target": target,
            "success": result.success,
            "error": result.measurement.error,
            "hops": serialize_route_hops(result.snapshot.hops) if result.snapshot is not None else [],
            "stability": {
                "is_stable": result.churn.is_stable if result.churn else None,
                "change_count": result.churn.change_count if result.churn else None,
                "snapshot_count": result.churn.snapshot_count if result.churn else None,
            },
        }
        console().print_json(json.dumps(payload))
        return 0 if result.success else 1

    c = console()
    if not result.success:
        c.print(f"Could not trace the route to {target}: {result.measurement.error or 'unknown error'}")
        return 1

    snapshot = result.snapshot
    assert snapshot is not None and result.churn is not None
    hop_count = len(snapshot.hops)
    c.print()
    c.print(route_table(snapshot.hops, title=f"Route to {target} ({hop_count} hop{'s' if hop_count != 1 else ''})"))
    c.print(
        f"  [dim]ASN data: {'available' if container.asn_lookup is not None else 'not configured'} "
        f"(drop an ASN database at ~/.netscope/ipasn.dat to enable AS numbers)[/dim]"
    )

    if result.churn.is_stable:
        c.print("Route stability: stable (single snapshot observed -- nothing to compare against yet)")
    else:
        c.print(f"Route stability: {result.churn.change_count} change(s) detected across {result.churn.snapshot_count} snapshots")
    c.print()
    return 0


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def run_history_command(container: Container, *, limit: int = 20, json_output: bool = False) -> int:
    """Runs the `history` command: recent measurement rounds
    reconstructed from the local SQLite log via `recent_rounds`."""
    rounds = recent_rounds(container, limit=limit)

    if json_output:
        payload = {
            "tool": "netscope",
            "report": "history",
            "rounds": [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "experience": serialize_experience(r.experience) if r.experience else None,
                    "measurements": [serialize_measurement(m) for m in r.measurements],
                }
                for r in rounds
            ],
        }
        console().print_json(json.dumps(payload))
        return 0

    c = console()
    if not rounds:
        c.print("No history yet -- run [bold]netscope diagnose[/bold] first; every round is saved locally in ~/.netscope/netscope.db")
        return 0

    from rich.table import Table as RichTable

    table = RichTable(title=f"Recent rounds (last {len(rounds)})", show_header=True, header_style="bold cyan", expand=True)
    table.add_column("When", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Level")
    table.add_column("Checks", justify="center")
    table.add_column("Detail", style="dim", overflow="fold")
    for r in rounds:
        score = f"{r.experience.score:.0f}" if r.experience else "—"
        level = r.experience.level.value if r.experience else "unknown"
        ok = sum(1 for m in r.measurements if m.success)
        failed = [m for m in r.measurements if not m.success]
        detail = "; ".join(f"{m.probe_type.value}: {truncate(m.error, 40)}" for m in failed) if failed else "all checks ok"
        table.add_row(format_time(r.timestamp), score, level, f"{ok}/{len(r.measurements)}", detail)
    c.print()
    c.print(table)
    c.print()
    return 0


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


def run_monitor_command(
    container: Container,
    config: NetScopeConfig,
    *,
    interval: float = 30.0,
    rounds: int = 0,
    gateway: str | None = AUTO_SENTINEL,
    sleep: Callable[[float], None] = time.sleep,
    round_runner: Callable[[Container, NetScopeConfig, str | None], tuple] | None = None,
) -> int:
    """Runs the `monitor` command: continuous measurement rounds with
    evidence-based incident detection.

    Every round goes through the same `run_measurement_round` use case
    as `diagnose` (same scoring, same evidence, same diagnosis), then
    the round's Diagnosis feeds a `MonitorSession` (app.monitoring),
    which wraps `core.incidents.detect_incident`: a problem must be
    sustained for `sustain_threshold` (3) consecutive rounds to OPEN an
    incident, and a sustained-healthy stretch to CLOSE one -- single
    noisy samples never flap incident state, and sustained
    INSUFFICIENT_EVIDENCE stretches neither open nor close anything.

    `sleep` and `round_runner` are injectable so tests can drive the
    loop deterministically without real time or real probes.

    Exit code: 0 after a normal run (Ctrl+C included -- stopping a
    monitor is not a failure), 1 only if a round itself crashes.
    """
    resolved_gateway = resolve_gateway(gateway, config) if gateway is not None else config.gateway
    session = MonitorSession()
    c = console()
    c.print()
    c.print(
        "[bold cyan]NetScope monitor[/bold cyan] [dim]— every {}s{} · sustained-problem incident detection · Ctrl+C to stop[/dim]".format(
            f"{interval:g}", f", {rounds} rounds" if rounds else ""
        )
    )
    c.print()

    executed = 0
    try:
        while rounds <= 0 or executed < rounds:
            executed += 1
            try:
                if round_runner is not None:
                    measurements, experience, diagnosis = round_runner(container, config, resolved_gateway)
                else:
                    measurements, experience, diagnosis = run_measurement_round(container, config, gateway=resolved_gateway)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad round must not kill the monitor
                c.print(f"[red]round {executed}: measurement failed: {exc}[/red]")
                if rounds > 0:
                    continue
                sleep(interval)
                continue

            for measurement in measurements:
                if container.measurement_repository is not None:
                    container.measurement_repository.save(measurement)

            failed = sum(1 for m in measurements if not m.success)
            c.print(
                f"  [{format_time(experience.timestamp)}]  score {experience.score:5.1f} ({experience.level.value})  "
                f"checks {len(measurements) - failed}/{len(measurements)} ok"
            )

            transition = session.process(diagnosis)
            if transition.kind is TransitionType.OPENED and transition.incident is not None and transition.incident.diagnosis is not None:
                c.print(f"  [bold red]⚠ incident OPENED[/bold red] — {explain(transition.incident.diagnosis)}")
            elif transition.kind is TransitionType.UPDATED and transition.incident is not None:
                c.print("  [red]• incident ongoing[/red]")
            elif transition.kind is TransitionType.CLOSED and transition.incident is not None:
                c.print(f"  [green]✓ incident CLOSED[/green] — lasted until at least {format_time(experience.timestamp)}")

            if executed < rounds or rounds <= 0:
                sleep(interval)
    except KeyboardInterrupt:
        c.print()
        c.print("[dim]monitor stopped[/dim]")

    if session.incident is not None and session.incident.is_active:
        c.print("[yellow]note: an incident was still open when monitoring stopped[/yellow]")
    c.print()
    return 0


# ---------------------------------------------------------------------------
# tui
# ---------------------------------------------------------------------------


def run_tui_command() -> int:
    """Runs the `tui` command: the Textual dashboard. The import is
    lazy so `pip install netscope` (without the `tui` extra) never
    breaks, and a missing textual produces an actionable message."""
    try:
        from netscope.ui.tui.app import NetScopeTUI
    except ImportError as exc:
        console().print("[red]The TUI requires the 'tui' extra:[/red] pip install netscope[tui]")
        console().print(f"[dim]({exc})[/dim]")
        return 1
    NetScopeTUI().run()
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    command = args.command or "diagnose"

    configure_logging(verbose=args.verbose)
    config = load_config()

    # The TUI builds its own container lazily (inside its worker loop),
    # so `netscope tui` never opens a SQLite file it doesn't need.
    if command == "tui":
        raise SystemExit(run_tui_command())

    container = build_container()

    if command == "diagnose":
        exit_code = run_diagnose_command(
            container,
            config,
            gateway=getattr(args, "gateway", None),
            json_output=getattr(args, "json", False),
        )
    elif command == "check":
        try:
            checks = parse_checks(args.checks)
        except ValueError as exc:
            parser.error(str(exc))
            return  # pragma: no cover -- parser.error() already exits
        exit_code = run_check_command(container, args.target, checks, json_output=args.json)
    elif command == "route":
        exit_code = run_route_command(
            container,
            args.target,
            resolve_hostnames=not args.no_resolve,
            json_output=args.json,
        )
    elif command == "history":
        exit_code = run_history_command(container, limit=args.limit, json_output=args.json)
    elif command == "monitor":
        exit_code = run_monitor_command(
            container,
            config,
            interval=args.interval,
            rounds=args.rounds,
            gateway=args.gateway,
        )
    else:  # pragma: no cover -- argparse rejects unknown commands itself
        parser.error(f"unrecognized command: {command}")
        return

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
