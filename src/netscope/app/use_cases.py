"""
netscope.app.use_cases

Application-level use cases: per-service monitoring (TASK-033) and
service-vs-general-connectivity comparison (TASK-034), per
future-roadmap.md's TASK-033/034 rows and module-boundaries.md's
Services section.

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
  `diagnose_service_with_connectivity` (TASK-034) is a pure comparison
  over two already-computed Diagnosis objects -- it doesn't call
  `diagnose()` again or invent a third classification path.
- Does not add Service persistence, a SQLite repository, or any
  REST/API surface -- TASK-032 deliberately left Service unpersisted,
  and this task doesn't "complete" that unless explicitly asked to.

TASK-034 SCOPE NOTE -- WHY THERE IS NO "RUN GENERAL CONNECTIVITY
CHECKS" HELPER HERE
---------------------------------------------------------------------
future-roadmap.md's TASK-034 row is exactly: "Compare a Service's
Diagnosis against a simultaneous generic-target Diagnosis to localize
service-specific vs. general issues," depending only on TASK-026 and
TASK-033 -- not on network-discovery/gateway-detection infrastructure.
`ui/cli.py` already has its own informal general-connectivity-
measurement pattern (gateway + public DNS + public CDN targets), but
consolidating or duplicating that here would be new, undeclared scope,
not a comparison. `diagnose_service_with_connectivity` therefore takes
an already-computed general-connectivity `Diagnosis | None` as a plain
argument -- however the caller obtained it (today, most naturally
`ui/cli.py`'s own measurement round) -- exactly mirroring the roadmap's
own wording ("against a simultaneous generic-target Diagnosis").
"""

from __future__ import annotations

from typing import Optional

from netscope.app.container import Container
from netscope.core.baseline import UserBaseline
from netscope.core.diagnosis import diagnose, evidence_from_latency
from netscope.core.models import Diagnosis, Evidence, Hypothesis, RawMeasurement, Service


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


def _merge_evidence(first: list[Evidence], second: list[Evidence]) -> list[Evidence]:
    """Concatenates two Evidence lists without duplicating an item
    already present in both -- the same small, order-preserving dedup
    `core.incidents._union_evidence` uses for its own, unrelated
    purpose (accumulating an Incident's evidence over time). Re-
    implemented locally rather than imported: that function is a
    private (`_`-prefixed) helper of a conceptually unrelated module
    (incident lifecycle, not service/connectivity comparison), and this
    is a three-line list utility, not diagnosis/evidence-generation
    logic that would be wrong to duplicate.
    """
    merged = list(first)
    for item in second:
        if item not in merged:
            merged.append(item)
    return merged


def diagnose_service_with_connectivity(
    service_diagnosis: Optional[Diagnosis],
    general_diagnosis: Optional[Diagnosis],
) -> Optional[Diagnosis]:
    """Compare a Service's own Diagnosis against a simultaneous,
    already-computed general-connectivity Diagnosis (TASK-034), to
    localize whether a problem is service-specific or a broader
    connectivity issue -- per future-roadmap.md's TASK-034 row and
    architecture-overview.md's SS10-11 principle that a diagnosis must
    be evidence-derived, never a fabricated boolean or a re-derived
    threshold comparison.

    Both arguments are `Diagnosis | None` -- `None` meaning "confirmed
    healthy," exactly `core.diagnosis.diagnose()`'s own return
    convention (TASK-026), reused as-is here rather than a second
    "is_healthy" boolean.

    This function never calls `diagnose()`, `evidence_from_latency()`,
    or any other evidence-generation/cause-selection logic itself -- it
    only recombines two already-classified Diagnosis objects. No new
    Hypothesis is ever selected; the result's classification is always
    either `service_diagnosis.classification`,
    `general_diagnosis.classification`, or
    `Hypothesis.INSUFFICIENT_EVIDENCE`.

    DECISION TABLE (each case named per this task's own roadmap brief)
    ------------------------------------------------------------------
    - Both healthy (`None`/`None`): nothing wrong anywhere -> `None`.
    - General has a *confirmed* problem (regardless of the service's own
      status): a broader connectivity failure is the more fundamental
      explanation and is never overridden by how the service happens to
      look -- returns `general_diagnosis`'s classification/confidence,
      with the service's own evidence merged in for transparency when
      it also has a Diagnosis to merge.
    - General confirmed healthy, service confirmed healthy -> `None`.
    - General confirmed healthy, service has a confirmed problem
      (Case B): genuinely service-specific -- returns
      `service_diagnosis`'s classification/evidence/confidence
      unchanged, with `Hypothesis.GENERAL_CONNECTIVITY_ISSUE` added to
      `ruled_out` (it was checked and excluded, which is exactly what
      `ruled_out` exists to record).
    - General confirmed healthy, service says
      `INSUFFICIENT_EVIDENCE`: the service's own uncertainty isn't
      resolved by general connectivity being fine -- returns
      `service_diagnosis` unchanged.
    - General is `INSUFFICIENT_EVIDENCE`, service confirmed healthy:
      the service itself is a confirmed answer regardless of the
      unrelated general uncertainty -> `None`.
    - General is `INSUFFICIENT_EVIDENCE`, service has a confirmed
      problem: THE KEY "no overconfident classification" case -- this
      function cannot confirm general connectivity is actually healthy,
      so it cannot confidently rule out a broader cause either,
      regardless of how clear-cut the service's own evidence looks.
      Mirrors the untested-gateway fix (TASK-026) one layer up: an
      unconfirmed alternative explanation blocks full confidence in the
      specific one. Returns `Hypothesis.INSUFFICIENT_EVIDENCE`,
      confidence `0.0`, with both diagnoses' evidence merged in.
    - Both `INSUFFICIENT_EVIDENCE`: nothing confirmed either way ->
      `Hypothesis.INSUFFICIENT_EVIDENCE`, confidence `0.0`.
    """
    general_status = general_diagnosis.classification if general_diagnosis is not None else None

    if general_status is not None and general_status is not Hypothesis.INSUFFICIENT_EVIDENCE:
        # A confirmed general-connectivity problem is the more
        # fundamental explanation, regardless of the service's own
        # status -- never reattributed to the service.
        if service_diagnosis is not None:
            return Diagnosis(
                classification=general_diagnosis.classification,
                evidence=_merge_evidence(general_diagnosis.evidence, service_diagnosis.evidence),
                confidence=general_diagnosis.confidence,
                ruled_out=list(general_diagnosis.ruled_out),
                timestamp=general_diagnosis.timestamp,
            )
        return general_diagnosis

    # From here, general connectivity is either confirmed healthy
    # (None) or itself INSUFFICIENT_EVIDENCE.

    if service_diagnosis is None:
        # The service itself is a confirmed answer -- an unrelated
        # unknown/healthy general status doesn't change that.
        return None

    if service_diagnosis.classification is Hypothesis.INSUFFICIENT_EVIDENCE:
        # The service's own uncertainty stands regardless of general
        # connectivity's status.
        return service_diagnosis

    if general_status is None:
        # General confirmed healthy, service has a confirmed problem:
        # genuinely service-specific.
        ruled_out = list(service_diagnosis.ruled_out)
        if Hypothesis.GENERAL_CONNECTIVITY_ISSUE not in ruled_out:
            ruled_out.append(Hypothesis.GENERAL_CONNECTIVITY_ISSUE)
        return Diagnosis(
            classification=service_diagnosis.classification,
            evidence=service_diagnosis.evidence,
            confidence=service_diagnosis.confidence,
            ruled_out=ruled_out,
            timestamp=service_diagnosis.timestamp,
        )

    # general_status is INSUFFICIENT_EVIDENCE and the service has a
    # confirmed problem: cannot rule out a broader cause we couldn't
    # confirm either way -- do not overconfidently blame the service.
    return Diagnosis(
        classification=Hypothesis.INSUFFICIENT_EVIDENCE,
        evidence=_merge_evidence(service_diagnosis.evidence, general_diagnosis.evidence),
        confidence=0.0,
        ruled_out=[],
        timestamp=service_diagnosis.timestamp,
    )
