"""
netscope.ui.cli

Thin CLI presentation layer. TASK-035 rebuilt this as a thin layer
calling `app` use cases only; TASK-036 extends it with the `diagnose`
subcommand ("`netscope diagnose` command wired to the diagnosis use
case", future-roadmap.md), the CLI's first named subcommand.

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
"""

from __future__ import annotations

import argparse
from typing import Optional

from netscope.app.config import NetScopeConfig, load_config
from netscope.app.container import Container, build_container, configure_logging
from netscope.app.use_cases import run_measurement_round
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


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    command = args.command or "diagnose"

    configure_logging(verbose=args.verbose)
    config = load_config()
    container = build_container()

    if command == "diagnose":
        exit_code = run_diagnose_command(container, config, gateway=getattr(args, "gateway", None))
    else:
        parser.error(f"unrecognized command: {command}")
        return  # pragma: no cover -- parser.error() already exits

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
