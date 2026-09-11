"""
netscope.ui.cli

Thin CLI presentation layer (TASK-035 rebuild), per future-roadmap.md's
TASK-035 row: "Rebuild ui/cli.py as a thin presentation layer calling
app use cases only." Parses arguments, configures logging, loads
config, builds the composition root, calls exactly one app use case
(app.use_cases.run_measurement_round), persists the resulting
measurements, and formats/prints the result -- no orchestration logic
(probe calls, scoring, diagnosis) lives here anymore; all of that moved
to app/use_cases.py, which is independently unit-testable with fake
adapters (closing the audit's "untestable orchestration" gap
architecture-decisions.md's CLI strategy entry names).

Per architecture-overview.md SS3's dependency table, `ui` may depend on
`app` and `core` (models, for typing/display only) -- never `adapters`
or `persistence` directly. This module's only persistence touchpoint
(`MeasurementRepository.open()`) is a deliberate, narrow exception:
saving what a round just measured is presentation-adjacent bookkeeping,
not business orchestration, and `run_measurement_round` (TASK-035)
itself intentionally stays persistence-agnostic (see its own
docstring) so this is the one place that decision is made. Everything
that decides *what the numbers mean* (scoring, evidence, diagnosis)
happens in `app`, not here.
"""

from __future__ import annotations

import argparse

from netscope.app.config import load_config
from netscope.app.container import build_container, configure_logging
from netscope.app.use_cases import run_measurement_round
from netscope.explanation.explainer import explain
from netscope.persistence.measurement_repository import MeasurementRepository


def build_arg_parser() -> argparse.ArgumentParser:
    """Builds the CLI's argument parser -- extracted from main() so
    argument-parsing behavior is testable without executing a real
    measurement round (architecture-overview.md SS3's `ui` test-strategy
    row: "argument-parsing edge cases")."""
    parser = argparse.ArgumentParser(prog="netscope", description="NetScope network diagnostics")
    parser.add_argument(
        "--gateway",
        help="IP of your local router/gateway, to localize local vs. upstream issues",
        default=None,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable verbose (INFO-level) logging",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    configure_logging(verbose=args.verbose)
    config = load_config()
    container = build_container()

    measurements, experience, diagnosis = run_measurement_round(container, config, gateway=args.gateway)

    store = MeasurementRepository.open()
    for measurement in measurements:
        store.save(measurement)
    store.close()

    print(f"\nExperience score: {experience.score}/100 ({experience.level.value})\n")
    if diagnosis is None:
        print("No issues detected.")
    else:
        print(explain(diagnosis))


if __name__ == "__main__":
    main()
