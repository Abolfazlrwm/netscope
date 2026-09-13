"""
netscope.ui.cli

Thin CLI presentation layer. TASK-035 rebuilt this as a thin layer
calling `app` use cases only; TASK-036 added the `diagnose` subcommand;
TASK-037 adds `route <target>` ("`netscope route <target>` wired to
traceroute + route analysis", future-roadmap.md).

Per architecture-overview.md SS3's dependency table, `ui` may depend on
`app` and `core` (models, for typing/display only) -- never `adapters`
or `persistence` directly. `run_diagnose_command` below reads
`container.measurement_repository` (TASK-035's `Container` field) to
save what a round just measured, rather than this module constructing
its own `MeasurementRepository` -- removing the one direct
`netscope.persistence` import TASK-035's version still had, since the
composition root (`build_container()`) already wires a repository for
exactly this purpose. Everything that decides *what the numbers mean*
(scoring, evidence, diagnosis) still happens in `app`/`core`, never
here.

WHY THERE IS NO NEW `app`-LEVEL "DIAGNOSIS USE CASE" FUNCTION
------------------------------------------------------------------
future-roadmap.md's TASK-036 row says the `diagnose` command is "wired
to the diagnosis use case" -- singular, definite article, not "a new
diagnosis use case." `app.use_cases.run_measurement_round` (TASK-035)
already *is* that use case: it runs a round of checks and returns a
`Diagnosis | None` (via `core.diagnosis.diagnose`), reusing existing
scoring/evidence/diagnosis logic entirely. Adding a second, differently
-named function that does the same thing (e.g. a `diagnose_now()`) would
be exactly the redundant-API proliferation this task's instructions
warn against ("Prefer one canonical application-level operation").
`diagnose` therefore wires directly to `run_measurement_round`, same as
the bare/no-subcommand invocation always has.

WHY `run_route_command` LIVES HERE, NOT IN `app/use_cases.py`
-------------------------------------------------------------------
TASK-037's own declared file scope is `ui/cli.py` only, explicitly
listing `app/use_cases.py` among the files not to touch absent a
proven, unavoidable incompatibility (none was found: the existing
`core.ports.Probe`/`ProbeRegistry`/`core.routing.analyze_route_churn`
APIs already support exactly what this command needs). This mirrors
`run_diagnose_command`'s own precedent one section above: a thin
per-command wiring function lives in `cli.py`, calling straight through
existing `core`/`container` APIs, without a dedicated `app`-level
function wrapping them. Nothing here decides *what a route change
means* -- `core.routing.analyze_route_churn` (TASK-021) does that,
completely unmodified and uninlined.

SINGLE-SNAPSHOT ROUTE ANALYSIS
-----------------------------------
`analyze_route_churn` (`core/routing.py`) is explicitly documented to
accept -- and meaningfully handle -- a sequence of exactly one
`RouteSnapshot`: "a single snapshot (nothing to compare against) is
reported as stable... not a claim that the route is guaranteed
unchanging beyond the observed window." `route <target>` calls it with
the single fresh snapshot this command obtains, honestly, rather than
adding route-history persistence to manufacture a longer sequence
(explicitly out of this task's scope).
"""

from __future__ import annotations

import argparse
from typing import Optional

from netscope.app.config import NetScopeConfig, load_config
from netscope.app.container import Container, build_container, configure_logging
from netscope.app.use_cases import run_measurement_round
from netscope.core.models import ProbeType, RouteSnapshot
from netscope.core.routing import analyze_route_churn
from netscope.explanation.explainer import explain


def build_arg_parser() -> argparse.ArgumentParser:
    """Builds the CLI's argument parser -- extracted from main() so
    argument-parsing behavior is testable without executing a real
    measurement round (architecture-overview.md SS3's `ui` test-strategy
    row: "argument-parsing edge cases").

    `--verbose`/`-v` is defined on a shared, help-suppressed parent
    parser (`_global_options`) and attached to every subcommand's own
    parser via `parents=` -- not to the top-level parser too, since
    argparse's `parents=` mechanism re-applies a shared option's
    *default* when the subparser runs, silently overwriting a value
    already parsed at the top level (a documented argparse quirk when
    the same `dest` is defined on both a parser and its subparser).
    Attaching it once, to each subcommand only, avoids that: `--verbose`
    is given after the subcommand name (`netscope diagnose -v`), which
    is enough for a single-subcommand CLI and avoids the trap entirely
    rather than working around it with extra state-merging logic.

    `diagnose` (TASK-036) is the CLI's first subcommand. Invoking
    `netscope` with no subcommand at all still runs the same diagnose
    behavior TASK-035 established as the bare invocation (see main()) --
    introducing subcommands does not remove that behavior, only gives
    it an explicit name alongside future subcommands (`route`,
    TASK-037, etc.).
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
        description="NetScope network diagnostics",
    )
    parser.set_defaults(verbose=False)
    subparsers = parser.add_subparsers(dest="command")

    diagnose_parser = subparsers.add_parser(
        "diagnose",
        help="run a diagnostic round (gateway/DNS/CDN checks) and report the result",
        parents=[global_options],
    )
    diagnose_parser.add_argument(
        "--gateway",
        help="IP of your local router/gateway, to localize local vs. upstream issues",
        default=None,
    )

    route_parser = subparsers.add_parser(
        "route",
        help="trace the route to a target and report route stability",
        parents=[global_options],
    )
    route_parser.add_argument("target", help="hostname or IP address to trace the route to")

    return parser


def run_diagnose_command(container: Container, config: NetScopeConfig, gateway: Optional[str] = None) -> int:
    """Runs the `diagnose` command: one measurement round via
    `run_measurement_round` (the existing, reused diagnosis use case),
    persists the resulting measurements if the container was built with
    a repository, and prints a human-readable result.

    Returns a process exit code. Always `0` on ordinary completion --
    a completed diagnosis, whether it found a problem or not, is not a
    CLI-level failure, so this doesn't invent an exit-code taxonomy
    beyond "the command ran" vs. argparse's own usage-error exit path.
    """
    measurements, experience, diagnosis = run_measurement_round(container, config, gateway=gateway)

    if container.measurement_repository is not None:
        for measurement in measurements:
            container.measurement_repository.save(measurement)

    print(f"\nExperience score: {experience.score}/100 ({experience.level.value})\n")
    if diagnosis is None:
        print("No issues detected.")
    else:
        print(explain(diagnosis))

    return 0


def run_route_command(container: Container, target: str) -> int:
    """Runs the `route` command: one traceroute via the existing
    `ProbeRegistry` (TASK-019's `TracerouteProbeAdapter`, reused
    unchanged -- this function never imports or calls `icmplib`
    directly), then route-stability analysis via the existing
    `core.routing.analyze_route_churn` (TASK-021, reused unchanged)
    over the single fresh `RouteSnapshot` obtained.

    Per the `Probe` Protocol's existing, established contract (see
    `probes/traceroute_probe.py`'s own docstring), a traceroute
    `RawMeasurement` carries its `RouteSnapshot` in
    `measurement.extra["route"]` -- this is not a new convention
    invented here, just read as-is.

    A `measurement.success is False` (e.g. permission denied, no hops
    obtained) is presented as a clean failure message, never a
    fabricated empty/successful route -- and returns exit code `1`,
    distinct from `run_diagnose_command`'s always-`0`: a diagnosis
    finding a *problem* still completed successfully, but a traceroute
    that could not produce any route data did not complete its job.
    """
    measurement = container.probe_registry.get(ProbeType.TRACEROUTE).run(target)

    if not measurement.success:
        print(f"Could not trace the route to {target}: {measurement.error or 'unknown error'}")
        return 1

    snapshot: RouteSnapshot = measurement.extra["route"]
    churn = analyze_route_churn([snapshot])

    hop_count = len(snapshot.hops)
    print(f"\nRoute to {target} ({hop_count} hop{'s' if hop_count != 1 else ''}):\n")
    for hop in snapshot.hops:
        address = hop.address or "*"
        rtt = f"{hop.avg_rtt_ms:.1f} ms" if hop.avg_rtt_ms is not None else "no response"
        print(f"  {hop.ttl:>2}  {address:<15}  {rtt}")

    if churn.is_stable:
        print("\nRoute stability: stable (single snapshot observed -- nothing to compare against yet)")
    else:
        print(f"\nRoute stability: {churn.change_count} change(s) detected across {churn.snapshot_count} snapshots")

    return 0


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    command = args.command or "diagnose"

    configure_logging(verbose=args.verbose)
    config = load_config()
    container = build_container()

    if command == "diagnose":
        exit_code = run_diagnose_command(container, config, gateway=getattr(args, "gateway", None))
    elif command == "route":
        exit_code = run_route_command(container, args.target)
    else:
        parser.error(f"unrecognized command: {command}")
        return  # pragma: no cover -- parser.error() already exits

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
