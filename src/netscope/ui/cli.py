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
- `netscope drift <target> [--days N] [--json]` (v0.15)
    The drift watch: the target's two most recent same-length history
    windows compared vital by vital (latency, loss, jitter, throughput)
    -- the slow leak no single check can flag, because the baseline it
    is judged against rots with the line it watches.
- `netscope monitor [--interval S] [--rounds N] [--gateway ...]`
    Continuous rounds with evidence-based incident detection: an
    incident only OPENS after a diagnosis is sustained for 3+ rounds
    (`core.incidents`), and only CLOSES after a sustained-healthy
    stretch -- single noisy samples never flap.
- `netscope speed [--url URL] [--seconds S] [--json]` (v0.3)
    One download-throughput measurement: median Mbps across fixed
    sampling windows, with the peak sample and bytes moved as evidence.
    A standalone command on purpose -- it never runs inside `diagnose`
    rounds (see bandwidth_probe.py's "WHAT THIS PROBE DOES NOT DO").
- `netscope tui`
    The live terminal dashboard (requires the `tui` extra:
    `pip install netscope[tui]`).
- `netscope completions bash|zsh|fish` (v0.11)
    Print a hand-written completion script for the given shell, ready
    to be redirected into that shell's completion directory.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version

from rich.table import Table

from netscope.app.config import NetScopeConfig, load_config
from netscope.app.container import Container, build_container, configure_logging
from netscope.app.gateway import AUTO_SENTINEL, resolve_gateway
from netscope.app.monitoring import MonitorSession, TransitionType
from netscope.app.reports import (
    build_diagnose_report,
    evidence_report_csv,
    evidence_report_html,
    evidence_report_markdown,
    evidence_report_prometheus,
    history_rounds_csv,
    serialize_evidence_report,
    serialize_experience,
    serialize_measurement,
    serialize_route_change,
    serialize_route_hops,
)
from netscope.app.use_cases import (
    build_evidence_report,
    check_service,
    check_target,
    list_services,
    recent_rounds,
    remove_service,
    run_drift_analysis,
    run_measurement_round,
    run_route_analysis,
    run_speed_test,
    save_service,
)
from netscope.core.models import Diagnosis, ExperienceEvent, ProbeType, RawMeasurement
from netscope.explanation.explainer import explain
from netscope.ui.completions import completion_script, completion_shells
from netscope.ui.render import (
    console,
    diagnosis_panel,
    format_time,
    measurements_table,
    route_stability_summary,
    route_table,
    sparkline,
    speed_panel,
    truncate,
)

# The five per-service check types, in a stable CLI-friendly order.
_CHECK_CHOICES = ("icmp", "dns", "tcp", "tls", "http")


def package_version() -> str:
    """Returns netscope's installed version, read from package metadata
    so the number always comes from the single source of truth (the
    pyproject.toml version baked in at install time) -- never a
    hardcoded copy that can drift. A raw, uninstalled checkout reports
    "unknown" instead of pretending to know."""
    try:
        return version("netscope")
    except PackageNotFoundError:
        return "unknown"


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
    parser.add_argument(
        "--version",
        action="version",
        version=f"netscope {package_version()}",
        help="show netscope's version and exit",
    )
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
    history.add_argument(
        "--csv",
        default=None,
        metavar="FILE",
        help="write the rounds' measurements as CSV to FILE (UTF-8) instead of the styled table; ignored under --json",
    )
    history.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    drift = subparsers.add_parser(
        "drift",
        help="compare the target's recent history window against the one before it, vital by vital",
        parents=[global_options],
    )
    drift.add_argument("target", help="hostname or IP whose stored history to watch")
    drift.add_argument(
        "--days",
        type=int,
        default=7,
        help="recent window length in days; the comparison window is the same length before it (default: 7)",
    )
    drift.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    monitor = subparsers.add_parser(
        "monitor",
        help="run continuous measurement rounds with incident detection (Ctrl+C to stop)",
        parents=[global_options],
    )
    monitor.add_argument("--interval", type=float, default=30.0, help="seconds between rounds (default: 30)")
    monitor.add_argument("--rounds", type=int, default=0, help="number of rounds before stopping (0 = until Ctrl+C)")
    monitor.add_argument("--gateway", default=AUTO_SENTINEL, help="gateway to probe: an IP, 'auto', or 'none'")

    speed = subparsers.add_parser(
        "speed",
        help="measure download throughput (median Mbps over a sampling window)",
        parents=[global_options],
    )
    speed.add_argument("--url", default=None, help="download URL to measure (default: the bandwidth_url config key)")
    speed.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="measurement window in seconds (default: the bandwidth_seconds config key)",
    )
    speed.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    report = subparsers.add_parser(
        "report",
        help="export a self-contained evidence report from the local measurement log (markdown, or --json)",
        parents=[global_options],
    )
    report.add_argument("--days", type=int, default=7, help="window length in days, ending now (default: 7)")
    report.add_argument("--target", default=None, help="restrict the report to one target (default: every target in the window)")
    report.add_argument("--out", default=None, help="write the markdown report to a file (UTF-8) instead of stdout")
    report.add_argument(
        "--html",
        default=None,
        metavar="FILE",
        help="write a self-contained HTML report (dark theme, inline SVG trends) to FILE (UTF-8); ignored under --json",
    )
    report.add_argument(
        "--csv",
        default=None,
        metavar="FILE",
        help="write the evidence table as CSV to FILE (UTF-8); ignored under --json",
    )
    report.add_argument(
        "--prometheus",
        default=None,
        metavar="FILE",
        help="write Prometheus text exposition metrics (textfile-collector format) to FILE (UTF-8); ignored under --json",
    )
    report.add_argument("--json", action="store_true", help="output machine-readable JSON instead of markdown")

    service = subparsers.add_parser(
        "service",
        help="manage the saved service list (persisted locally; shared by the CLI and the TUI services board)",
        parents=[global_options],
    )
    service_sub = service.add_subparsers(dest="service_command", required=True)

    svc_add = service_sub.add_parser("add", help="save (or update) a service in the curated list")
    svc_add.add_argument("name", help="label for the service, e.g. 'My VPN' -- the saved list's identity")
    svc_add.add_argument("host", help="hostname or IP the service's checks target")
    svc_add.add_argument(
        "--checks",
        default=",".join(_CHECK_CHOICES),
        help="comma-separated subset of checks to enable (default: all five)",
    )

    svc_list = service_sub.add_parser("list", help="list the saved services")
    svc_list.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    svc_remove = service_sub.add_parser("remove", help="remove a service from the saved list")
    svc_remove.add_argument("name", help="exact saved name to remove")

    svc_check = service_sub.add_parser("check", help="run a saved service's checks now and report the result")
    svc_check.add_argument("name", help="exact saved name to check")
    svc_check.add_argument("--json", action="store_true", help="output machine-readable JSON instead of styled text")

    completions = subparsers.add_parser(
        "completions",
        help="print a shell completion script to stdout, ready for the shell's completion directory",
        parents=[global_options],
    )
    completions.add_argument(
        "shell",
        choices=completion_shells(),
        help="the shell to print completions for",
    )

    subparsers.add_parser(
        "tui",
        help='launch the live terminal dashboard (requires: pip install -e ".[tui]")',
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

    _print_check_result(measurements, experience, diagnosis, title=f"Checks against {target}")
    return 0


def _print_check_result(measurements: list[RawMeasurement], experience: ExperienceEvent, diagnosis: Diagnosis | None, *, title: str) -> None:
    """The one presentation path for `check`-shaped results -- the
    `netscope check` command and `netscope service check` render through
    here, so the two never drift (same score line, same measurements
    table, same diagnosis panel)."""
    c = console()
    c.print()
    c.print(f"Experience score: {experience.score:.0f}/100 ({experience.level.value})")
    c.print(measurements_table(measurements, title=title))
    c.print()
    c.print(diagnosis_panel(diagnosis))
    c.print()


def _ordered_service_checks(enabled: set[ProbeType]) -> list[ProbeType]:
    """The CLI's fixed display order for service checks (the same order
    `--checks` documents), never storage order -- storage sorts
    alphabetically for byte-stable rows (service_repository.py), display
    keeps the documented icmp,dns,tcp,tls,http order."""
    return [check for check in (ProbeType(choice) for choice in _CHECK_CHOICES) if check in enabled]


# ---------------------------------------------------------------------------
# service (v0.7) -- TASK-032's storage half reaches the CLI
# ---------------------------------------------------------------------------


def run_service_add_command(container: Container, name: str, host: str, checks: set[ProbeType]) -> int:
    """Runs `service add`: validate + upsert via `save_service`, then
    one honest line -- "saved" for a new entry, "updated" for an
    upserted one, with the effective check set spelled out so a typo in
    `--checks` never saves silently."""
    try:
        service, created = save_service(name, host, checks, container)
    except (ValueError, RuntimeError) as exc:
        console().print(f"[red]{exc}[/red]")
        return 1

    checks_text = ", ".join(check.value for check in _ordered_service_checks(service.enabled_checks)) or "none yet"
    if created:
        console().print(f"Saved service [bold]{service.name}[/] — {service.host} (checks: {checks_text})")
        console().print(f"[dim]run it anytime:[/] netscope service check {service.name}")
    else:
        console().print(f"Updated service [bold]{service.name}[/] — host {service.host} (checks: {checks_text})")
    return 0


def run_service_list_command(container: Container, *, json_output: bool = False) -> int:
    """Runs `service list`: the curated list exactly as persisted,
    alphabetical by name -- never merged with log-derived targets here
    (the TUI board does that join, visibly; the CLI shows the truth of
    the saved list alone)."""
    services = list_services(container)

    if json_output:
        payload = {
            "tool": "netscope",
            "report": "services",
            "services": [
                {
                    "name": svc.name,
                    "host": svc.host,
                    "checks": [check.value for check in _ordered_service_checks(svc.enabled_checks)],
                }
                for svc in services
            ],
        }
        console().print_json(json.dumps(payload))
        return 0

    c = console()
    if not services:
        c.print("No saved services yet.")
        c.print("[dim]save one:[/] netscope service add NAME HOST [--checks icmp,dns,tcp,tls,http]")
        c.print("[dim]the netscope tui services board also shows every target the log already holds[/]")
        return 0

    table = Table(title="Saved services")
    table.add_column("Name", style="bold")
    table.add_column("Host")
    table.add_column("Checks")
    for svc in services:
        checks_text = ", ".join(check.value for check in _ordered_service_checks(svc.enabled_checks)) or "[dim]none yet[/]"
        table.add_row(svc.name, svc.host, checks_text)
    c.print()
    c.print(table)
    c.print(f"\n[dim]{len(services)} service{'s' if len(services) != 1 else ''} · run one:[/] netscope service check NAME")
    return 0


def run_service_remove_command(container: Container, name: str) -> int:
    """Runs `service remove`: one honest line -- "removed" when the row
    existed, "no such service" (plus what IS saved) when it didn't, so
    a typo'd name is never a silent success."""
    try:
        removed = remove_service(name, container)
    except RuntimeError as exc:
        console().print(f"[red]{exc}[/red]")
        return 1

    if removed:
        console().print(f"Removed service [bold]{name}[/]")
        return 0

    saved = [svc.name for svc in list_services(container)]
    c = console()
    if saved:
        c.print(f"[red]no saved service named '{name}'[/] [dim](saved: {', '.join(saved)})[/]")
    else:
        c.print(f"[red]no saved service named '{name}'[/] [dim](the saved list is empty)[/]")
    return 1


def run_service_check_command(container: Container, name: str, *, json_output: bool = False) -> int:
    """Runs `service check`: the saved Service's own curated checks
    (name, host and check set come from the persisted entry, never
    re-typed), through the same `check_service` pipeline and the same
    presentation as `netscope check` -- one pipeline, two entry points.
    Results persist exactly like `netscope check` does."""
    saved = {svc.name: svc for svc in list_services(container)}
    service = saved.get(name)
    if service is None:
        c = console()
        if saved:
            c.print(f"[red]no saved service named '{name}'[/] [dim](saved: {', '.join(sorted(saved))})[/]")
        else:
            c.print(f"[red]no saved service named '{name}'[/] [dim](the saved list is empty -- add one with netscope service add)[/]")
        return 1

    measurements, experience, diagnosis = check_service(service, container)

    if container.measurement_repository is not None:
        for measurement in measurements:
            container.measurement_repository.save(measurement)

    if json_output:
        report = build_diagnose_report(measurements, experience, diagnosis, gateway=None)
        report["report"] = "service-check"
        report["service"] = service.name
        report["target"] = service.host
        console().print_json(json.dumps(report))
        return 0

    _print_check_result(measurements, experience, diagnosis, title=f"{service.name} — checks against {service.host}")
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
        churn = result.churn
        payload = {
            "tool": "netscope",
            "report": "route",
            "target": target,
            "success": result.success,
            "error": result.measurement.error,
            "hops": serialize_route_hops(result.snapshot.hops) if result.snapshot is not None else [],
            "stability": {
                "is_stable": churn.is_stable if churn else None,
                "change_count": churn.change_count if churn else None,
                "snapshot_count": churn.snapshot_count if churn else None,
                "prior_runs": result.prior_runs,
                "window_start": churn.window_start.isoformat() if churn is not None and churn.window_start is not None else None,
                "last_change": serialize_route_change(churn.last_change) if churn is not None and churn.last_change is not None else None,
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

    c.print(f"Route stability: {route_stability_summary(result.churn, result.prior_runs)}")
    if not result.churn.is_stable and result.churn.change_count > 1:
        c.print(
            f"  [dim]{result.churn.change_count} path change(s) across the last {result.churn.snapshot_count} runs -- "
            "the latest is shown; --json carries them all[/dim]"
        )
    c.print()
    return 0


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def run_history_command(
    container: Container,
    *,
    limit: int = 20,
    json_output: bool = False,
    csv_out: str | None = None,
) -> int:
    """Runs the `history` command: recent measurement rounds
    reconstructed from the local SQLite log via `recent_rounds`.

    Renderings: the styled table (default), machine-readable JSON
    (`--json`), and -- v0.17 -- a CSV file export (`--csv FILE`): one
    row per stored measurement with its round denormalized onto every
    row, written with explicit UTF-8 for the same reason every other
    exported file is. Like the report's `--html`, the export is ignored
    under `--json` -- one primary rendering per invocation."""
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

    if csv_out is not None:
        # Explicit UTF-8 and newline="": the CSV is an export artifact
        # someone opens in a spreadsheet, and Windows consoles default
        # to legacy code pages just as they always have.
        with open(csv_out, "w", encoding="utf-8", newline="") as fh:
            fh.write(history_rounds_csv(rounds))
        measurements = sum(len(r.measurements) for r in rounds)
        tail = " -- the log has no rounds yet, so the file carries the header only" if not rounds else ""
        console().print(f"CSV export ({measurements} measurement(s) in {len(rounds)} round(s)) written to [bold]{csv_out}[/bold]{tail}")
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
    scores = [r.experience.score for r in rounds if r.experience]
    trend = sparkline(scores)
    if trend:
        c.print(f"  score trend  [bold cyan]{trend}[/bold cyan]  low {min(scores):.0f} · high {max(scores):.0f} over {len(scores)} scored rounds")
    c.print()
    return 0


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


# Per-vital display metadata, keyed by the core verdict's vital name:
# the human label (with the window statistic named -- median vs mean is
# part of the honest story) and the unit each number wears. Presentation
# only -- the values and verdicts come from core untouched.
_DRIFT_VITAL_LABELS = {
    "latency": ("Latency (median)", "ms"),
    "packet_loss": ("Packet loss (mean)", "pp"),
    "jitter": ("Jitter (median)", "ms"),
    "throughput": ("Throughput (median)", "Mbps"),
}

_DRIFT_VERDICT_STYLES = {
    "degrading": "bold red",
    "improving": "green",
    "stable": "cyan",
    "insufficient_history": "dim",
}

# The table is the human rendering, so the machine verdict words get
# human words where the machine's word is not already a sentence
# ("degrading" reads fine; "insufficient_history" is JSON vocabulary).
# The JSON payload keeps the canonical words untouched. The label stays
# short enough for the narrowest verdict column it renders in -- a
# wrapped verdict cell would smear the table's borders across the
# collapse of two rows.
_DRIFT_VERDICT_LABELS = {
    "degrading": "degrading",
    "improving": "improving",
    "stable": "stable",
    "insufficient_history": "no data",
}


def _format_drift_change(vital: str, change_abs: float | None, change_pct: float | None) -> str:
    """The Change cell: the absolute move in the vital's own unit plus
    the relative one; loss speaks in percentage points only -- a ratio
    from a 0.2% floor is the meaningless number core/drift.py refuses
    to make a signal, and the table must not dress it up as one. An
    unmeasurable change (either half absent) renders as an em dash --
    absence disclosed, not zero claimed."""
    if change_abs is None:
        return "--"
    unit = _DRIFT_VITAL_LABELS[vital][1]
    absolute = f"{change_abs:+.1f}{unit}"
    if change_pct is None or vital == "packet_loss":
        return absolute
    return f"{absolute} ({change_pct:+.0f}%)"


def run_drift_command(container: Container, target: str, *, days: int = 7, json_output: bool = False) -> int:
    """Runs the `drift` command: the target's two history windows
    compared via the `run_drift_analysis` use case (never window math
    re-derived here), then presentation.

    The exit code is always `0` when the analysis ran: an insufficient
    verdict is an honest finding about coverage, not a failed command --
    the same convention `history` sets for an empty log."""
    report = run_drift_analysis(target, container, recent_days=days)

    if json_output:
        payload = {
            "tool": "netscope",
            "report": "drift",
            "target": target,
            "recent_days": report.recent_days,
            "window": {
                "start": report.window_start.isoformat(),
                "boundary": report.boundary.isoformat(),
                "end": report.end.isoformat(),
            },
            "overall": report.overall,
            "vitals": [
                {
                    "vital": v.vital,
                    "verdict": v.verdict,
                    "older": v.older_stat,
                    "recent": v.recent_stat,
                    "change_abs": v.change_abs,
                    "change_pct": v.change_pct,
                    "older_samples": v.older_samples,
                    "recent_samples": v.recent_samples,
                    "detail": v.detail,
                }
                for v in report.vitals
            ],
            "evidence": list(report.evidence),
        }
        console().print_json(json.dumps(payload))
        return 0

    c = console()
    total_samples = sum(v.older_samples + v.recent_samples for v in report.vitals)
    c.print()
    if total_samples == 0:
        c.print(
            f"No stored history for [bold]{target}[/bold] in the last {2 * days} days -- "
            f"run [bold]netscope check {target}[/bold] regularly; the watch needs two windows of stored rounds."
        )
        c.print()
        return 0

    c.print(
        f"[bold]Drift watch: {target}[/bold]  [dim]"
        f"older window {report.window_start:%Y-%m-%d %H:%M} -> {report.boundary:%Y-%m-%d %H:%M} | "
        f"recent window {report.boundary:%Y-%m-%d %H:%M} -> {report.end:%Y-%m-%d %H:%M}[/dim]"
    )

    table = Table(title=None, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Vital", no_wrap=True)
    table.add_column(f"Older ({days}d)", justify="right")
    table.add_column(f"Recent ({days}d)", justify="right")
    table.add_column("Change", justify="right")
    table.add_column("Verdict")
    for v in report.vitals:
        label, unit = _DRIFT_VITAL_LABELS[v.vital]
        style = _DRIFT_VERDICT_STYLES[v.verdict]
        older = "--" if v.older_stat is None else f"{v.older_stat:.1f}{unit}"
        recent = "--" if v.recent_stat is None else f"{v.recent_stat:.1f}{unit}"
        verdict_label = _DRIFT_VERDICT_LABELS[v.verdict]
        table.add_row(label, older, recent, _format_drift_change(v.vital, v.change_abs, v.change_pct), f"[{style}]{verdict_label}[/]")
    c.print(table)

    overall_style = _DRIFT_VERDICT_STYLES[report.overall]
    overall_label = _DRIFT_VERDICT_LABELS[report.overall]
    c.print(f"Overall: [{overall_style}]{overall_label}[/]")
    for sentence in report.evidence:
        c.print(f"  [dim]{sentence}[/dim]")
    c.print("  [dim]windows are inclusive on both ends; readings outside them are ignored -- --json carries every sample count[/dim]")
    c.print()
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def run_report_command(
    container: Container,
    *,
    days: int = 7,
    target: str | None = None,
    out: str | None = None,
    html_out: str | None = None,
    csv_out: str | None = None,
    prometheus_out: str | None = None,
    json_output: bool = False,
    until: datetime | None = None,
) -> int:
    """Runs the `report` command: rebuilds the persisted measurement
    log into one self-contained evidence document (`build_evidence_report`)
    and emits it as markdown (stdout, or a UTF-8 file via `--out`) or
    machine-readable JSON (`--json`), and -- v0.10 -- as a standalone
    HTML page via `--html FILE`: the same facts, same caps, same
    disclosures, dressed with inline SVG trends and a score gauge, no
    scripts and no external assets.

    v0.17 adds two more export renderings of the same report: `--csv
    FILE` (the evidence tables flattened to one row per shown reading)
    and `--prometheus FILE` (the text exposition format a textfile
    collector or scraper reads). Both are ignored under `--json`, like
    `--html` before them -- one primary rendering per invocation.

    The markdown goes to stdout through plain `print` on purpose: it is
    an export artifact someone pipes, redirects or attaches -- it must
    reach the terminal byte-for-byte, not re-wrapped by the styled
    console. The HTML file is written with explicit UTF-8 for the same
    reason the markdown file is.

    `until` passes straight through to `build_evidence_report`'s own
    injectable clock: the CLI never sets it (the window ends now, as
    always), but the tests seed fixed-date rows and pin the clock to
    match -- seed dates and window end must age TOGETHER, not one
    without the other.
    """
    try:
        report = build_evidence_report(container, days=days, target=target, until=until)
    except ValueError as exc:
        console().print(f"[red]{exc}[/red]")
        return 2

    if json_output:
        console().print_json(json.dumps(serialize_evidence_report(report)))
        return 0

    markdown = evidence_report_markdown(report)
    if out is not None:
        # Explicit UTF-8: the report carries arrows, middle dots and
        # ellipses, and Windows consoles default to legacy code pages.
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        console().print(f"Evidence report ({report.measurements} measurement(s), {report.failures} failed) written to [bold]{out}[/bold]")
    else:
        print(markdown)

    if html_out is not None:
        with open(html_out, "w", encoding="utf-8") as fh:
            fh.write(evidence_report_html(report))
        console().print(
            f"HTML report ({report.measurements} measurement(s), {report.failures} failed) written to [bold]{html_out}[/bold] -- open it in any browser, it needs nothing else"
        )

    if csv_out is not None:
        # The truncation the human formats disclose in prose is
        # disclosed here, in the command's own voice: a CSV is data and
        # carries no prose of its own, so the count of rows past the
        # table cap rides on the confirmation line instead.
        truncated = sum(t.truncated for t in report.target_reports)
        note = f" ({truncated} row(s) past the table cap omitted)" if truncated else ""
        with open(csv_out, "w", encoding="utf-8", newline="") as fh:
            fh.write(evidence_report_csv(report))
        console().print(
            f"CSV evidence table ({report.measurements} measurement(s), {report.failures} failed) written to [bold]{csv_out}[/bold]{note}"
        )

    if prometheus_out is not None:
        with open(prometheus_out, "w", encoding="utf-8", newline="") as fh:
            fh.write(evidence_report_prometheus(report))
        console().print(
            f"Prometheus metrics ({report.measurements} measurement(s), {report.failures} failed) written to [bold]{prometheus_out}[/bold] -- text exposition format, ready for a textfile collector or scraper"
        )
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
# speed (v0.3)
# ---------------------------------------------------------------------------


def run_speed_command(
    container: Container,
    config: NetScopeConfig,
    url: str | None = None,
    seconds: float | None = None,
    *,
    json_output: bool = False,
) -> int:
    """Runs the `speed` command: one download-throughput measurement,
    rendered as a headline panel (or flat JSON). Exit code 0 only for a
    successful measurement -- a failed speed test is a real failure of
    the command's one job, not a soft warning."""
    measurement = run_speed_test(container, config, url=url, seconds=seconds)
    if json_output:
        from netscope.probes.bandwidth_probe import summarize

        console().print_json(json.dumps(summarize(measurement)))
        return 0 if measurement.success else 1
    console().print(speed_panel(measurement))
    if not measurement.success:
        console().print(f"[dim]target: {measurement.target}[/dim]")
        return 1
    return 0


# ---------------------------------------------------------------------------
# tui
# ---------------------------------------------------------------------------


def run_tui_command() -> int:
    """Runs the `tui` command: the Textual dashboard. The import is
    lazy so `pip install netscope` (without the `tui` extra) never
    breaks, and a missing textual produces an actionable message.

    The shell receives the loaded config (v0.16): enrichment wiring in
    the lazy container build -- geo database path, the user's home,
    the ASN source -- reads the same config.toml every command reads,
    not a default-constructed guess."""
    try:
        from netscope.ui.tui.app import NetScopeTUI
    except ImportError as exc:
        console().print("[red]The TUI requires the 'tui' extra:[/red] pip install netscope[tui]")
        console().print(f"[dim]({exc})[/dim]")
        return 1
    NetScopeTUI(config=load_config()).run()
    return 0


# ---------------------------------------------------------------------------
# completions
# ---------------------------------------------------------------------------


def run_completions_command(shell: str) -> int:
    """Runs the `completions` command: prints the requested shell's
    completion script verbatim. Plain print(), never the rich console --
    the output is redirected into a file or sourced by a shell, so a
    single ANSI escape sequence would corrupt it."""
    print(completion_script(shell), end="")
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    command = args.command or "diagnose"

    # Completion scripts are pure static text: print them before any
    # logging setup, config read or container build -- none of that
    # belongs in a tab-completion pipeline.
    if command == "completions":
        raise SystemExit(run_completions_command(args.shell))

    configure_logging(verbose=args.verbose)
    config = load_config()

    # The TUI builds its own container lazily (inside its worker loop),
    # so `netscope tui` never opens a SQLite file it doesn't need.
    if command == "tui":
        raise SystemExit(run_tui_command())

    container = build_container(config=config)

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
        exit_code = run_history_command(container, limit=args.limit, json_output=args.json, csv_out=args.csv)
    elif command == "drift":
        exit_code = run_drift_command(container, args.target, days=args.days, json_output=args.json)
    elif command == "report":
        exit_code = run_report_command(
            container,
            days=args.days,
            target=args.target,
            out=args.out,
            html_out=args.html,
            csv_out=args.csv,
            prometheus_out=args.prometheus,
            json_output=args.json,
        )
    elif command == "monitor":
        exit_code = run_monitor_command(
            container,
            config,
            interval=args.interval,
            rounds=args.rounds,
            gateway=args.gateway,
        )
    elif command == "speed":
        exit_code = run_speed_command(
            container,
            config,
            url=getattr(args, "url", None),
            seconds=getattr(args, "seconds", None),
            json_output=getattr(args, "json", False),
        )
    elif command == "service":
        sub = args.service_command
        if sub == "add":
            try:
                checks = parse_checks(args.checks)
            except ValueError as exc:
                parser.error(str(exc))
                return  # pragma: no cover -- parser.error() already exits
            exit_code = run_service_add_command(container, args.name, args.host, checks)
        elif sub == "list":
            exit_code = run_service_list_command(container, json_output=args.json)
        elif sub == "remove":
            exit_code = run_service_remove_command(container, args.name)
        elif sub == "check":
            exit_code = run_service_check_command(container, args.name, json_output=args.json)
        else:  # pragma: no cover -- argparse rejects unknown subcommands itself
            parser.error(f"unrecognized service command: {sub}")
            return
    else:  # pragma: no cover -- argparse rejects unknown commands itself
        parser.error(f"unrecognized command: {command}")
        return

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
