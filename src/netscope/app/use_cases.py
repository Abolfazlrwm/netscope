"""
netscope.app.use_cases

Application-level use cases: per-service monitoring (TASK-033),
service-vs-general-connectivity comparison (TASK-034), and the general
measurement round (TASK-035) that replaces `ui/cli.py`'s old inline
orchestration -- per future-roadmap.md's TASK-033/034/035 rows,
module-boundaries.md's Services section, and
architecture-overview.md SS3's `app` responsibility row, which names
`run_measurement_round()` as one of `app`'s use cases explicitly.

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
`run_measurement_round` below) -- exactly mirroring the roadmap's own
wording ("against a simultaneous generic-target Diagnosis").

TASK-035 SCOPE NOTE -- WHY run_measurement_round() ALSO SCORES/DIAGNOSES
-----------------------------------------------------------------------------
architecture-overview.md SS3 lists four illustrative `app` use-case
names together: `run_measurement_round()`, `get_experience()`,
`diagnose_now()`, `list_recent_incidents()`. TASK-035's own scope is
"CLI foundation" (config/logging/composition-root, rebuilding the
*existing* CLI behavior as a thin layer) -- not TASK-036's "Diagnostic
command" (which explicitly introduces a *new* `netscope diagnose`
command). Since today's CLI behavior already bundles running, scoring,
and diagnosing into one round-trip (this predates the numbered task
sequence), and TASK-035's job is to rebuild that *existing* behavior
thinly rather than remove functionality for TASK-036 to re-add,
`run_measurement_round()` here covers all three -- matching current
observable CLI behavior exactly, just properly layered and testable.
Splitting it into the finer `get_experience()`/`diagnose_now()`
decomposition the doc's name list gestures at is left for whichever
later task (TASK-036 or otherwise) actually needs that finer grain;
pre-emptively guessing that decomposition now would be exactly the
kind of speculative abstraction this task's instructions warn against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from netscope.app.config import NetScopeConfig
from netscope.app.container import Container
from netscope.core.baseline import UserBaseline
from netscope.core.diagnosis import diagnose, evidence_from_latency
from netscope.core.models import (
    Diagnosis,
    Evidence,
    ExperienceEvent,
    Hypothesis,
    ProbeType,
    RawMeasurement,
    RouteSnapshot,
    Service,
)
from netscope.core.routing import RouteChurnResult, analyze_route_churn
from netscope.core.scoring import score_measurements


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
    baseline: UserBaseline | None = None,
) -> Diagnosis | None:
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
    evidence = [evidence_from_latency(f"service_{measurement.probe_type.value}_latency", measurement, baseline) for measurement in measurements]
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
    service_diagnosis: Diagnosis | None,
    general_diagnosis: Diagnosis | None,
) -> Diagnosis | None:
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

    if general_diagnosis is not None and general_status is not None and general_status is not Hypothesis.INSUFFICIENT_EVIDENCE:
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
    assert general_diagnosis is not None  # INSUFFICIENT_EVIDENCE status implies a Diagnosis carried it
    return Diagnosis(
        classification=Hypothesis.INSUFFICIENT_EVIDENCE,
        evidence=_merge_evidence(service_diagnosis.evidence, general_diagnosis.evidence),
        confidence=0.0,
        ruled_out=[],
        timestamp=service_diagnosis.timestamp,
    )


def run_measurement_round(
    container: Container,
    config: NetScopeConfig,
    gateway: str | None = None,
    baseline: UserBaseline | None = None,
) -> tuple[list[RawMeasurement], ExperienceEvent, Diagnosis | None]:
    """Run one general-connectivity measurement round, scored against
    the user's personal baseline -- now actually *persisted* (v0.2):

    1. `baseline` resolution: an explicitly passed baseline wins;
       otherwise the container's `BaselineRepository` loads the learned
       baseline from disk (fresh empty one on first run); with no
       repository wired, a fresh in-memory baseline is used -- the
       pre-0.2 behavior, preserved for tests/fakes.
    2. Measurements, in order:
       - the local gateway via ICMP, if `gateway` (or `config.gateway`
         when `gateway` isn't given) is set -- None otherwise;
       - `config.public_dns_target` via ICMP;
       - `config.dns_lookup_domain` via DNS;
       - `config.public_cdn_url` via HTTP.
    3. Scoring/diagnosis compare this round against the baseline loaded
       in step 1 -- the round is judged against *past* history, never
       against itself.
    4. Only then are this round's successful readings observed into the
       baseline (Welford update) and saved -- so tomorrow's rounds are
       judged against slightly more history than today's. This is the
       load/observe/persist wiring architecture-overview.md SS10 always
       assigned to the app layer; scoring and diagnosis themselves stay
       read-only against the baseline, exactly as their own docstrings
       require.

    Returns `(measurements, experience, diagnosis)`. Does not persist
    `measurements` itself -- saving those stays the caller's job (same
    convention as before); only the *baseline* is persisted here.
    """
    baseline = baseline if baseline is not None else container.load_baseline()
    gateway = gateway if gateway is not None else config.gateway
    registry = container.probe_registry

    local_gateway = registry.get(ProbeType.ICMP).run(gateway) if gateway else None
    public_dns = registry.get(ProbeType.ICMP).run(config.public_dns_target)
    dns_lookup = registry.get(ProbeType.DNS).run(config.dns_lookup_domain)
    public_cdn = registry.get(ProbeType.HTTP).run(config.public_cdn_url)

    measurements = [m for m in (local_gateway, public_dns, dns_lookup, public_cdn) if m is not None]

    experience = score_measurements(measurements, baseline)

    evidence = [
        evidence_from_latency("gateway_latency", local_gateway, baseline),
        evidence_from_latency("dns_latency", public_dns, baseline),
        evidence_from_latency("destination_latency", public_cdn, baseline),
    ]
    diagnosis = diagnose(evidence)

    observe_measurements(baseline, measurements)
    if container.baseline_repository is not None:
        container.baseline_repository.save(baseline)

    return measurements, experience, diagnosis


def observe_measurements(baseline: UserBaseline, measurements: list[RawMeasurement]) -> None:
    """Record successful readings into `baseline` (the app layer's
    baseline-observe responsibility, per architecture-overview.md SS10).

    Only successful measurements with an actual reading are observed:
    a failed probe has no latency to learn from, and observing failure
    artifacts (None) would corrupt the running statistics. Failed
    rounds influence diagnosis through the evidence layer instead."""
    for m in measurements:
        if not m.success:
            continue
        if m.latency_ms is not None:
            baseline.observe_latency(m.target, m.latency_ms)
        if m.packet_loss_pct is not None:
            baseline.observe_packet_loss(m.target, m.packet_loss_pct)


# ---------------------------------------------------------------------------
# Route analysis (v0.2) -- previously inline in ui/cli.py's route command,
# promoted to an app use case so the TUI and CLI share one implementation
# and so ASN/hostname enrichment (adapter territory) stays out of ui.
# ---------------------------------------------------------------------------


@dataclass
class RouteAnalysisResult:
    """Everything one `netscope route` / TUI route view needs: the raw
    traceroute measurement, the enriched snapshot, and the churn
    analysis over it. `snapshot`/`churn` are None exactly when the
    traceroute itself failed (never a fabricated empty route)."""

    measurement: RawMeasurement
    snapshot: RouteSnapshot | None = None
    churn: RouteChurnResult | None = None

    @property
    def success(self) -> bool:
        return self.measurement.success and self.snapshot is not None


def run_route_analysis(
    target: str,
    container: Container,
    *,
    resolve_hostnames: bool = True,
) -> RouteAnalysisResult:
    """Trace the route to `target` and enrich it: ASN per hop (when an
    offline ASN database is wired in the container), reverse-DNS hostnames
    (when `resolve_hostnames` and a resolver is wired), then route-stability
    analysis via the existing `core.routing.analyze_route_churn`.

    Every capability degrades gracefully: no ASN database -> hops keep
    asn=None; resolution failure -> hostname=None; failed traceroute ->
    RouteAnalysisResult with snapshot/churn None (the CLI reports the
    error, never an empty route)."""
    measurement = container.probe_registry.get(ProbeType.TRACEROUTE).run(target)
    if not measurement.success:
        return RouteAnalysisResult(measurement=measurement)

    snapshot = measurement.extra.get("route")
    if snapshot is None:
        return RouteAnalysisResult(measurement=measurement)

    _enrich_snapshot(snapshot, container, resolve_hostnames=resolve_hostnames)
    churn = analyze_route_churn([snapshot])
    return RouteAnalysisResult(measurement=measurement, snapshot=snapshot, churn=churn)


def _enrich_snapshot(snapshot: RouteSnapshot, container: Container, *, resolve_hostnames: bool) -> None:
    """Fill in RouteHop lookup fields the traceroute probe deliberately
    leaves None (see RouteHop's own docstring): ASN from the optional
    offline database, hostnames from the optional reverse-DNS resolver.
    Mutates the snapshot's hops in place -- enrichment is a display
    concern and RouteSnapshot.signature() is immune to it (hop addresses
    only)."""
    if container.asn_lookup is not None:
        for hop in snapshot.hops:
            if hop.address is None or hop.asn is not None:
                continue
            try:
                result = container.asn_lookup.lookup(hop.address)
            except ValueError:
                continue  # hostname hop, not an IP -- nothing to look up
            if result.matched and result.asn is not None:
                hop.asn = f"AS{result.asn}"

    if resolve_hostnames and container.hostname_resolver is not None:
        hostnames = container.hostname_resolver.resolve_all([hop.address for hop in snapshot.hops])
        for hop in snapshot.hops:
            if hop.address is not None and hop.hostname is None:
                hop.hostname = hostnames.get(hop.address)


# ---------------------------------------------------------------------------
# Target checks (v0.2) -- `netscope check <host>`: any user-supplied target
# through the exact same per-service checks `diagnose_service` uses.
# ---------------------------------------------------------------------------


def check_target(
    target: str,
    container: Container,
    checks: frozenset[ProbeType] | set[ProbeType],
    baseline: UserBaseline | None = None,
) -> tuple[list[RawMeasurement], ExperienceEvent, Diagnosis | None]:
    """Run `checks` (a subset of the five service check types) against
    an arbitrary user-supplied target, and score + diagnose the results
    -- `netscope check <host>`'s use case.

    Reuses `run_service_checks` and the `service_*` metric vocabulary
    unchanged (so diagnosis categorizes failures as SERVICE_ISSUE --
    from the general pipeline's perspective, "this one target is
    broken" IS the service-issue question), and additionally returns an
    ExperienceEvent so the CLI can show the 0-100 score for the target.
    A failed check is CRITICAL evidence on its own; an empty `checks`
    set yields INSUFFICIENT_EVIDENCE, never a fabricated healthy
    result."""
    baseline = baseline if baseline is not None else UserBaseline()
    service = Service(name=target, host=target, enabled_checks=set(checks))
    measurements = run_service_checks(service, container)
    experience = score_measurements(measurements, baseline)
    evidence = [evidence_from_latency(f"service_{m.probe_type.value}_latency", m, baseline) for m in measurements]
    return measurements, experience, diagnose(evidence)


# ---------------------------------------------------------------------------
# History (v0.2) -- `netscope history` and the TUI history tab: recent
# measurement rounds grouped out of the persisted measurement log.
# ---------------------------------------------------------------------------


@dataclass
class HistoryRound:
    """One reconstructed measurement round: measurements that landed
    within one time window, plus the experience score recomputed for
    them against the *current* baseline (a history view's honest
    approximation -- the historical baseline at each round's time is
    not reconstructible from the measurement log alone)."""

    timestamp: datetime
    measurements: list[RawMeasurement] = field(default_factory=list)
    experience: ExperienceEvent | None = None

    @property
    def all_success(self) -> bool:
        return bool(self.measurements) and all(m.success for m in self.measurements)


def recent_rounds(
    container: Container,
    limit: int = 50,
    *,
    max_gap_seconds: int = 120,
) -> list[HistoryRound]:
    """Reconstruct the most recent measurement rounds from the
    persistence log, oldest first, for `netscope history` and the TUI
    history tab.

    Measurements are stored flat (one row each); a "round" is
    reconstructed by grouping consecutive measurements whose timestamps
    are at most `max_gap_seconds` apart. Requires a wired
    measurement_repository -- without one (bare test containers) there
    is no history, and the result is an empty list rather than an
    error."""
    if container.measurement_repository is None:
        return []

    # Fetch a generous window: rounds average ~4 measurements, and
    # grouping discards partial leading/trailing rounds.
    raw = container.measurement_repository.latest(limit=limit * 10)
    if not raw:
        return []

    ascending = sorted(raw, key=lambda m: m.timestamp)
    rounds: list[HistoryRound] = []
    current: HistoryRound | None = None
    gap = timedelta(seconds=max_gap_seconds)
    for m in ascending:
        if current is None or (m.timestamp - current.timestamp) > gap:
            current = HistoryRound(timestamp=m.timestamp)
            rounds.append(current)
        current.measurements.append(m)

    baseline = container.load_baseline()
    for round_ in rounds[-limit:]:
        round_.experience = score_measurements(round_.measurements, baseline)
        # The re-computed score describes THAT round, stamped with its own
        # time -- score_measurements stamps utcnow() (its convention for
        # live rounds), which would mislabel historical rows.
        if round_.experience is not None:
            round_.experience.timestamp = round_.timestamp
    return rounds[-limit:]
