"""
netscope.app.use_cases

Application-level use cases (TASK-033), per future-roadmap.md's
TASK-033 row: "app-level use case running a Service's enabled checks
and aggregating results," and module-boundaries.md's Services section:
"aggregate the Measurements for one Service's enabled checks into a
per-service view ... Outputs: A per-service Diagnosis."

This is the first file in `app/use_cases.py` -- `app/__init__.py`
(TASK-005) described this package's purpose ("expose use cases ...
as the only API the ui package is allowed to call") but deferred
actually writing any until the pieces they'd orchestrate existed. Those
pieces (ProbeRegistry/Container, core.diagnosis, core.baseline,
Service) are now all in place.

WHY THIS LIVES IN app, NOT core
------------------------------------
This module imports and wires together a concrete adapter registry
(`app.container.Container`, which itself holds `ProbeRegistry` --
concrete `adapters.probes.*` instances) alongside core ports/logic
(`core.diagnosis`, `core.baseline`). adr-002-probe-adapter-strategy.md
designates `app` as "the one package allowed to import both a core port
and a concrete netscope.adapters.* class together" -- `core` itself
must never do this (it would violate core's infrastructure-independence,
the same rule core/diagnosis.py, core/incidents.py, core/scoring.py all
already document for themselves).

WHAT THIS MODULE DOES NOT DO
---------------------------------
- Does not implement any probe logic itself -- every check runs through
  `container.probe_registry.get(probe_type).run(...)`, the exact same
  adapters any other measurement round uses (module-boundaries.md:
  "must not duplicate probe logic").
- Does not reimplement diagnosis -- `diagnose_service` calls
  `core.diagnosis.diagnose()` directly on Evidence built via
  `core.diagnosis.evidence_from_latency()` (TASK-027), never a second,
  parallel diagnosis engine or a raw-threshold comparison.
- Does not compare a Service's Diagnosis against general connectivity --
  that comparison is future-roadmap.md's own, separately-scoped
  TASK-034 ("same file, extended"), not this task's.
- Does not add Service persistence, a SQLite repository, or any
  REST/API surface -- TASK-032 deliberately left Service unpersisted,
  and this task doesn't "complete" that unless explicitly asked to.
"""

from __future__ import annotations

from typing import Optional

from netscope.app.container import Container
from netscope.core.baseline import UserBaseline
from netscope.core.diagnosis import diagnose, evidence_from_latency
from netscope.core.models import Diagnosis, RawMeasurement, Service


def run_service_checks(service: Service, container: Container) -> list[RawMeasurement]:
    """Execute exactly `service.enabled_checks` against `service.host`,
    one call per enabled check, via `container.probe_registry` -- never
    every possible `ProbeType`, and never a check the Service didn't
    enable.

    `service.enabled_checks` is a `set[ProbeType]` (core/models.py,
    TASK-032), so the order measurements are returned in is not
    meaningful -- callers that care which check a given
    `RawMeasurement` came from should match on `.probe_type`, not list
    position. Each adapter is looked up fresh per call via
    `container.probe_registry.get(probe_type)`, so a `ProbeType` with no
    registered adapter surfaces the registry's own defined failure mode
    (`ProbeNotRegisteredError`, adapters/probes/registry.py) rather than
    this module inventing a second one.
    """
    return [container.probe_registry.get(probe_type).run(service.host) for probe_type in service.enabled_checks]


def diagnose_service(
    service: Service,
    container: Container,
    baseline: Optional[UserBaseline] = None,
) -> Optional[Diagnosis]:
    """Run `service`'s enabled checks and aggregate the resulting
    measurements into a single per-service `Diagnosis` (or `None` if
    every enabled check came back healthy -- `core.diagnosis.diagnose`'s
    own convention, reused as-is here).

    Each `RawMeasurement` becomes one piece of `Evidence` via
    `core.diagnosis.evidence_from_latency`, using
    `f"service_{probe_type.value}_latency"` as the metric name. This
    reuses `core.diagnosis`'s own existing metric-prefix vocabulary
    unchanged: any metric starting with `"service_"` already categorizes
    as `Hypothesis.SERVICE_ISSUE` (see `core/diagnosis.py`'s
    `_METRIC_CATEGORY`, anticipating this exact use since TASK-026) --
    nothing about that module needed to change for this task.

    `baseline` defaults to a fresh, empty `UserBaseline()` if not given
    -- the same "no BaselineRepository-backed load/persist wiring exists
    yet" status quo `ui/cli.py` already documents for its own scoring
    and evidence generation (TASK-025/027); this task doesn't add
    baseline persistence either.

    An empty `service.enabled_checks` produces no Evidence, which
    `diagnose()` already resolves to `Hypothesis.INSUFFICIENT_EVIDENCE`
    (its own existing empty-input behavior) -- not a fabricated healthy
    result and not a special case this function needs to add.
    """
    baseline = baseline if baseline is not None else UserBaseline()
    measurements = run_service_checks(service, container)
    evidence = [
        evidence_from_latency(f"service_{measurement.probe_type.value}_latency", measurement, baseline)
        for measurement in measurements
    ]
    return diagnose(evidence)
