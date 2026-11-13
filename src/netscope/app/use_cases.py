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
from netscope.core.asn import asn_extra
from netscope.core.baseline import UserBaseline, compare_throughput
from netscope.core.diagnosis import diagnose, evidence_from_jitter, evidence_from_latency
from netscope.core.geo import geo_extra
from netscope.core.loss import LOSS_CONFIRMATION_ROUNDS, merge_loss_rounds, needs_loss_confirmation
from netscope.core.models import (
    Diagnosis,
    Evidence,
    ExperienceEvent,
    Hypothesis,
    ProbeErrorType,
    ProbeType,
    RawMeasurement,
    RouteSnapshot,
    Service,
    utcnow,
)
from netscope.core.ports import Probe
from netscope.core.routing import RouteChurnResult, analyze_route_churn
from netscope.core.scoring import score_measurements


def enrich_with_geo(measurements: list[RawMeasurement], container: Container) -> None:
    """Attach each measurement's target geography to its `extra` as the
    canonical `extra["geo"]` dict (v0.12, TASK-023) -- best-effort,
    in place, before scoring/persistence so every downstream surface
    (rich detail, --json extra block, the persisted log the HTML
    report rebuilds from) sees the same single copy of the fact.

    HOW AN IP IS FOUND: an IP-literal target is used as-is; otherwise
    a DNS probe's already-recorded answers are scanned (the check
    pipeline resolved that host seconds ago -- re-asking the resolver
    would be waste); otherwise one stdlib getaddrinfo call. Nothing
    resolves -> nothing attached, silently: enrichment is display
    metadata, and a "geo lookup failed" warning on every offline
    round would be noise, not honesty.

    GRACEFUL DEGRADATION, the same doctrine as `_enrich_snapshot`:
    no GeoLookup or resolver wired -> no-op; an unresolvable target,
    a corrupt database, or an address the database does not know ->
    no-op for that measurement (RuntimeError here covers the lookup
    adapter's own unavailable-database failure mode -- enrichment is
    best-effort and must never turn a geo hiccup into a failed
    check). A measurement that already carries `geo` is never
    re-enriched (idempotent, like the loss-confirmation marker).
    """
    geo = container.geo_lookup
    resolver = container.ip_resolver
    if geo is None or resolver is None:
        return
    for m in measurements:
        if "geo" in m.extra:
            continue
        ip = resolver.resolve(m.target, m.extra)
        if ip is None:
            continue
        try:
            info = geo.lookup(ip)
        except (ValueError, RuntimeError):
            continue
        if info is None:
            continue
        m.extra["geo"] = geo_extra(info)


def enrich_with_asn(measurements: list[RawMeasurement], container: Container) -> None:
    """Attach each measurement's target network ownership to its `extra`
    as the canonical `extra["asn"]` dict (v0.14) -- best-effort, in
    place, the exact discipline `enrich_with_geo` established for
    geography: one single copy of the fact, written before scoring and
    persistence, so every downstream surface (rich detail, --json
    extra block, the evidence reports) reads the same record.

    HOW AN IP IS FOUND: the same order geo enrichment uses -- an
    IP-literal target wins; else a DNS probe's already-recorded
    answers are scanned; else one stdlib getaddrinfo call. When the
    geo pass already resolved this target this round, its recorded
    `extra["geo"]["ip"]` is reused -- one resolution per fact per
    round, never a second lookup for a number already on screen.

    GRACEFUL DEGRADATION, the shared doctrine: no AsnLookup or
    resolver wired -> no-op; an unresolvable target, a corrupt
    source, or an address neither source knows -> no-op for that
    measurement. A measurement that already carries `asn` is never
    re-enriched (idempotent, like `geo` and the loss marker)."""
    asn_lookup = container.asn_lookup
    resolver = container.ip_resolver
    if asn_lookup is None or resolver is None:
        return
    for m in measurements:
        if "asn" in m.extra:
            continue
        geo = m.extra.get("geo")
        ip = geo.get("ip") if isinstance(geo, dict) else None
        if not isinstance(ip, str):
            ip = resolver.resolve(m.target, m.extra)
        if not ip:
            continue
        try:
            info = asn_lookup.lookup(ip)
        except (ValueError, RuntimeError):
            continue
        if info is None or info.asn is None:
            continue
        m.extra["asn"] = asn_extra(info)


def run_with_loss_confirmation(probe: Probe, target: str) -> RawMeasurement:
    """Run `probe` once against `target`; if the reading shows the
    partial-loss shape worth a second opinion (core.loss's policy), run
    `LOSS_CONFIRMATION_ROUNDS` extra sweeps and fold them into one merged
    measurement -- v0.9's "confirm before you report" rule.

    This helper owns the *running* half of the policy only, which is why
    it lives in app: execution is orchestration (the same reason the
    baseline observe/persist wiring lives here), while the judgment of
    when a reading warrants more evidence and how sweeps fold together
    stays pure in `core.loss`. Probes whose readings aren't loss-shaped
    (clean sweeps, dead targets, HTTP/DNS answers) come back untouched
    after exactly one run -- confirmation costs nothing until there is
    something to confirm.

    The registry's own failure mode is untouched: the caller resolves the
    probe via `container.probe_registry.get(...)`, so an unregistered
    `ProbeType` still surfaces `ProbeNotRegisteredError` before this
    helper is ever invoked.
    """
    measurement = probe.run(target)
    if not needs_loss_confirmation(measurement):
        return measurement
    confirmations = [probe.run(target) for _ in range(LOSS_CONFIRMATION_ROUNDS)]
    return merge_loss_rounds([measurement, *confirmations])


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

    Every run goes through `run_with_loss_confirmation` (v0.9): a sweep
    that comes back with partial loss gets its confirmation sweeps and
    reports the merged window, so a saved service's check never reads a
    one-off stray drop as a property of the link.
    """
    return [run_with_loss_confirmation(container.probe_registry.get(probe_type), service.host) for probe_type in service.enabled_checks]


def diagnose_service(
    service: Service,
    container: Container,
    baseline: UserBaseline | None = None,
) -> Diagnosis | None:
    """Run `service`'s enabled checks and aggregate the resulting
    measurements into a single per-service `Diagnosis` (or `None` if
    every enabled check came back healthy -- `core.diagnosis.diagnose`'s
    own convention, reused as-is here).

    Each `RawMeasurement` becomes one or two pieces of `Evidence` via
    `core.diagnosis`: `evidence_from_latency` with
    `f"service_{probe_type.value}_latency"` as the metric name, and
    (v0.13) `evidence_from_jitter` with the `_jitter` twin. For check
    types that never report a variation reading the jitter Evidence is
    tested-but-zero-confidence -- never a fabricated healthy or bad
    claim. This reuses `core.diagnosis`'s own existing metric-prefix
    vocabulary unchanged: any metric starting with `"service_"` already
    categorizes as `Hypothesis.SERVICE_ISSUE` (see
    `core/diagnosis.py`'s `_METRIC_CATEGORY`, anticipating this exact
    use since TASK-026) -- nothing about that module needed to change
    for this task.

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
    evidence = []
    for measurement in measurements:
        evidence.append(evidence_from_latency(f"service_{measurement.probe_type.value}_latency", measurement, baseline))
        evidence.append(evidence_from_jitter(f"service_{measurement.probe_type.value}_jitter", measurement, baseline))
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
       Both ICMP sweeps run through `run_with_loss_confirmation`
       (v0.9): a sweep reporting partial loss is re-swept and the rounds
       merge into one measurement whose loss number is the window
       average and whose `extra` records whether the loss reproduced --
       scoring, the baseline and the diagnosis all see the confirmed
       reading, never the raw first suspicion.
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

    local_gateway = run_with_loss_confirmation(registry.get(ProbeType.ICMP), gateway) if gateway else None
    public_dns = run_with_loss_confirmation(registry.get(ProbeType.ICMP), config.public_dns_target)
    dns_lookup = registry.get(ProbeType.DNS).run(config.dns_lookup_domain)
    public_cdn = registry.get(ProbeType.HTTP).run(config.public_cdn_url)

    measurements = [m for m in (local_gateway, public_dns, dns_lookup, public_cdn) if m is not None]

    enrich_with_geo(measurements, container)
    enrich_with_asn(measurements, container)

    experience = score_measurements(measurements, baseline)

    evidence = [
        evidence_from_latency("gateway_latency", local_gateway, baseline),
        evidence_from_jitter("gateway_jitter", local_gateway, baseline),
        evidence_from_latency("dns_latency", public_dns, baseline),
        evidence_from_jitter("dns_jitter", public_dns, baseline),
        evidence_from_latency("destination_latency", public_cdn, baseline),
        evidence_from_jitter("destination_jitter", public_cdn, baseline),
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
    rounds influence diagnosis through the evidence layer instead. The
    same rule covers jitter (v0.13): only sweeps that actually report a
    variation reading are observed, so a probe type without one simply
    leaves that store untouched.
    """
    for m in measurements:
        if not m.success:
            continue
        if m.latency_ms is not None:
            baseline.observe_latency(m.target, m.latency_ms)
        if m.packet_loss_pct is not None:
            baseline.observe_packet_loss(m.target, m.packet_loss_pct)
        if m.jitter_ms is not None:
            baseline.observe_jitter(m.target, m.jitter_ms)


# ---------------------------------------------------------------------------
# Route analysis (v0.2) -- previously inline in ui/cli.py's route command,
# promoted to an app use case so the TUI and CLI share one implementation
# and so ASN/hostname enrichment (adapter territory) stays out of ui.
# v0.8 added route memory: the churn verdict spans the target's stored
# history, not just the run that produced it.
# ---------------------------------------------------------------------------

# How many stored route observations one run compares against (its own
# snapshot appended as the window's newest member). A ceiling, not a
# target: targets traced less often simply compare against everything
# they have. 20 traceroutes is a generous evidence window without
# letting one target's history grow the comparison unboundedly.
ROUTE_HISTORY_LIMIT = 20


@dataclass
class RouteAnalysisResult:
    """Everything one `netscope route` / TUI route view needs: the raw
    traceroute measurement, the enriched snapshot, and the churn
    analysis over it. `snapshot`/`churn` are None exactly when the
    traceroute itself failed (never a fabricated empty route).

    `prior_runs` (v0.8) is how many stored observations preceded this
    run's snapshot in the churn comparison -- 0 for a first observation
    or when no RouteRepository is wired; the UI's "stable" and "changed"
    verdicts are only meaningful when it is positive."""

    measurement: RawMeasurement
    snapshot: RouteSnapshot | None = None
    churn: RouteChurnResult | None = None
    prior_runs: int = 0

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

    ROUTE MEMORY (v0.8)
    ---------------------
    When a RouteRepository is wired, the churn verdict finally spans
    runs: the target's stored history (the most recent
    ROUTE_HISTORY_LIMIT observations, oldest first) is loaded BEFORE
    this run's snapshot is stored, the analysis compares
    `[*history, this run]`, and only then is the new snapshot appended
    to the store -- compare-then-observe, the same ordering doctrine
    v0.4 established for throughput baselines (a run must never
    contaminate its own comparison). `RouteAnalysisResult.prior_runs`
    tells the UI how many stored observations preceded this one: 0
    means a first observation ("nothing to compare against yet" is then
    the honest verdict), not an absence of memory.

    Every capability degrades gracefully: no ASN database -> hops keep
    asn=None; resolution failure -> hostname=None; failed traceroute ->
    RouteAnalysisResult with snapshot/churn None (the CLI reports the
    error, never an empty route); no RouteRepository wired -> v0.7's
    single-snapshot behavior, unchanged (an automatic observation is
    not a user-issued save: there is nothing to refuse to lose, unlike
    the curated service list's explicit writes).
    """
    measurement = container.probe_registry.get(ProbeType.TRACEROUTE).run(target)
    if not measurement.success:
        return RouteAnalysisResult(measurement=measurement)

    snapshot = measurement.extra.get("route")
    if snapshot is None:
        return RouteAnalysisResult(measurement=measurement)

    _enrich_snapshot(snapshot, container, resolve_hostnames=resolve_hostnames)

    route_repository = container.route_repository
    history: list[RouteSnapshot] = route_repository.load_recent(target, limit=ROUTE_HISTORY_LIMIT) if route_repository is not None else []
    churn = analyze_route_churn([*history, snapshot])
    if route_repository is not None:
        route_repository.save(snapshot)

    return RouteAnalysisResult(measurement=measurement, snapshot=snapshot, churn=churn, prior_runs=len(history))


def _enrich_snapshot(snapshot: RouteSnapshot, container: Container, *, resolve_hostnames: bool) -> None:
    """Fill in RouteHop lookup fields the traceroute probe deliberately
    leaves None (see RouteHop's own docstring): ASN from the optional
    offline database, hostnames from the optional reverse-DNS resolver,
    and -- v0.12 -- each hop's country from the optional GeoIP database
    (`RouteHop.country`'s documented purpose since TASK-020; hops carry
    a place name, never a distance -- a per-hop distance question has
    no answer the user asked for). Mutates the snapshot's hops in
    place -- enrichment is a display concern and
    RouteSnapshot.signature() is immune to it (hop addresses only)."""
    if container.asn_lookup is not None:
        for hop in snapshot.hops:
            if hop.address is None or hop.asn is not None:
                continue
            try:
                result = container.asn_lookup.lookup(hop.address)
            except ValueError:
                continue  # hostname hop, not an IP -- nothing to look up
            if result is not None and result.asn is not None:
                hop.asn = f"AS{result.asn}"
                if hop.organization is None and result.organization is not None:
                    # The v0.14 payoff: RouteHop.organization's first
                    # real population since TASK-020 declared the
                    # field lookup-ready -- the mmdb source's name,
                    # never the text table's (it answers None, and a
                    # placeholder name would be fabrication).
                    hop.organization = result.organization

    if resolve_hostnames and container.hostname_resolver is not None:
        hostnames = container.hostname_resolver.resolve_all([hop.address for hop in snapshot.hops])
        for hop in snapshot.hops:
            if hop.address is not None and hop.hostname is None:
                hop.hostname = hostnames.get(hop.address)

    if container.geo_lookup is not None:
        for hop in snapshot.hops:
            if hop.address is None or hop.country is not None:
                continue
            try:
                info = container.geo_lookup.lookup(hop.address)
            except (ValueError, RuntimeError):
                continue  # hostname hop, or a database that cannot answer
            if info is not None and info.place is not None and info.place.country is not None:
                hop.country = info.place.country


# ---------------------------------------------------------------------------
# Target checks (v0.2) -- `netscope check <host>`: any user-supplied target
# through the exact same per-service checks `diagnose_service` uses.
# ---------------------------------------------------------------------------


def check_service(
    service: Service,
    container: Container,
    baseline: UserBaseline | None = None,
) -> tuple[list[RawMeasurement], ExperienceEvent, Diagnosis | None]:
    """Run `service`'s own enabled checks against `service.host`, and
    score + diagnose the results -- the saved-service form of
    `check_target` (v0.7): the name, host and check set all come from
    the Service itself, so a curated entry is checked exactly as it was
    defined, not re-transcribed at the call site.

    Same pipeline and same honesty rules as `check_target`, by
    construction -- one is defined in terms of the other, never
    duplicated: reuses `run_service_checks` and the `service_*` metric
    vocabulary unchanged (so diagnosis categorizes failures as
    SERVICE_ISSUE), and additionally returns an ExperienceEvent so the
    CLI can show the 0-100 score for the service. A failed check is
    CRITICAL evidence on its own; an empty `enabled_checks` set yields
    INSUFFICIENT_EVIDENCE, never a fabricated healthy result."""
    baseline = baseline if baseline is not None else UserBaseline()
    measurements = run_service_checks(service, container)
    enrich_with_geo(measurements, container)
    enrich_with_asn(measurements, container)
    experience = score_measurements(measurements, baseline)
    evidence = []
    for m in measurements:
        evidence.append(evidence_from_latency(f"service_{m.probe_type.value}_latency", m, baseline))
        evidence.append(evidence_from_jitter(f"service_{m.probe_type.value}_jitter", m, baseline))
    return measurements, experience, diagnose(evidence)


def check_target(
    target: str,
    container: Container,
    checks: frozenset[ProbeType] | set[ProbeType],
    baseline: UserBaseline | None = None,
) -> tuple[list[RawMeasurement], ExperienceEvent, Diagnosis | None]:
    """Run `checks` (a subset of the five service check types) against
    an arbitrary user-supplied target, and score + diagnose the results
    -- `netscope check <host>`'s use case.

    A thin wrapper over `check_service` that names the ad-hoc Service
    after the target itself: per the domain model, an unsaved check IS
    a one-off Service -- "data someone supplied" -- just one that was
    never curated into the persisted list (v0.7's storage half is what
    makes the distinction between the two real).

    Reuses `run_service_checks` and the `service_*` metric vocabulary
    unchanged (so diagnosis categorizes failures as SERVICE_ISSUE --
    from the general pipeline's perspective, "this one target is
    broken" IS the service-issue question), and additionally returns an
    ExperienceEvent so the CLI can show the 0-100 score for the target.
    A failed check is CRITICAL evidence on its own; an empty `checks`
    set yields INSUFFICIENT_EVIDENCE, never a fabricated healthy
    result."""
    return check_service(Service(name=target, host=target, enabled_checks=set(checks)), container, baseline)


# ---------------------------------------------------------------------------
# Service persistence (v0.7) -- TASK-032's storage half: the curated
# service list finally has a home, and these are the only ways in.
# ---------------------------------------------------------------------------


def save_service(
    name: str,
    host: str,
    checks: set[ProbeType] | frozenset[ProbeType],
    container: Container,
) -> tuple[Service, bool]:
    """Validate and persist one curated Service, and return
    `(service, created)` -- `created` False meaning an entry with the
    same name already existed and was updated in place (the
    ServiceRepository port's documented upsert semantics), so the CLI
    can say "saved" versus "updated" honestly without a second query.

    Validation happens through the domain model itself: constructing
    the `Service` raises the domain's own ValueError on a check set
    outside `SERVICE_CHECK_TYPES`, and this function additionally
    rejects an empty/blank name or host -- a service with no name can't
    be addressed by remove/check, and one with no host can't be probed;
    neither is a service, just a half-typed line.

    Raises RuntimeError when no ServiceRepository is wired: unlike the
    measurement log (where "measured but not persisted" is an honest,
    documented degradation), a management operation whose entire
    purpose is persistence must not silently drop its write.
    """
    if container.service_repository is None:
        raise RuntimeError("no ServiceRepository wired -- services cannot be saved without persistence")
    if not name.strip():
        raise ValueError("service name must not be empty")
    if not host.strip():
        raise ValueError("service host must not be empty")
    service = Service(name=name, host=host, enabled_checks=set(checks))
    created = container.service_repository.get(service.name) is None
    container.service_repository.save(service)
    return service, created


def remove_service(name: str, container: Container) -> bool:
    """Remove one curated Service by exact name; True iff it existed --
    the caller can say "removed" versus "no such service" honestly.

    RuntimeError when no ServiceRepository is wired (same reasoning as
    `save_service`)."""
    if container.service_repository is None:
        raise RuntimeError("no ServiceRepository wired -- services cannot be removed without persistence")
    return container.service_repository.remove(name)


def list_services(container: Container) -> list[Service]:
    """The curated service list, alphabetical by name.

    An empty list when nothing has been curated yet -- and also when no
    ServiceRepository is wired, the same tolerant read convention
    `Container.load_baseline` established for a missing persistence
    layer: reading a list that doesn't exist yields an empty list,
    while writing without storage would silently lose data (which is
    why save/remove are strict, not tolerant)."""
    if container.service_repository is None:
        return []
    return container.service_repository.load_all()


# ---------------------------------------------------------------------------
# Bandwidth (v0.3) -- `netscope speed`: a download-throughput measurement
# as its own use case, deliberately NOT part of the measurement round.
# ---------------------------------------------------------------------------


def run_speed_test(
    container: Container,
    config: NetScopeConfig,
    *,
    url: str | None = None,
    seconds: float | None = None,
) -> RawMeasurement:
    """Run one bandwidth measurement and return its RawMeasurement.

    Deliberately a standalone use case, not a step of
    run_measurement_round: a speed test downloads for seconds at a
    time, which would make every routine `netscope diagnose` round
    slow and data-hungry, and its result is throughput evidence rather
    than a latency/loss reading -- feeding it into the round would put
    it into scoring/baseline paths it explicitly does not belong in
    (see bandwidth_probe.py's "WHAT THIS PROBE DOES NOT DO").

    `url`/`seconds` come from the CLI flags when given, otherwise fall
    back to the config defaults (`bandwidth_url`/`bandwidth_seconds`)
    -- the same flags-override-config convention the `--gateway` flag
    follows. The adapter is fetched through the registry like every
    other probe, so tests can swap in a fake the usual way.

    Since v0.4 a successful test also participates in the throughput
    baseline (and only there -- never the latency/loss metrics):
    1. the result is compared against what was learned BEFORE this run
       (compare-then-observe, the same order the round path uses) and,
       when the baseline has an opinion, the comparison rides along on
       the measurement's `extra` as `baseline_mean_mbps` /
       `baseline_samples` / `deviation_pct` / `verdict`;
    2. the result is then observed into `UserBaseline.throughput` and
       the baseline is saved -- so tomorrow's speed tests are judged
       against today's.
    """
    resolved_url = url if url is not None else config.bandwidth_url
    resolved_seconds = seconds if seconds is not None else config.bandwidth_seconds
    measurement = container.probe_registry.get(ProbeType.BANDWIDTH).run(resolved_url, duration_s=resolved_seconds)
    mbps = measurement.extra.get("throughput_mbps")
    if measurement.success and isinstance(mbps, (int, float)) and mbps > 0:
        baseline = container.load_baseline()
        comparison = compare_throughput(baseline, resolved_url, float(mbps))
        if comparison is not None:
            measurement.extra.update(
                {
                    "baseline_mean_mbps": comparison.baseline_mean_mbps,
                    "baseline_samples": comparison.baseline_samples,
                    "deviation_pct": comparison.deviation_pct,
                    "verdict": comparison.verdict,
                }
            )
        baseline.observe_throughput(resolved_url, float(mbps))
        if container.baseline_repository is not None:
            container.baseline_repository.save(baseline)
    return measurement


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


def _group_rounds(
    measurements: list[RawMeasurement],
    *,
    max_gap_seconds: int,
) -> list[HistoryRound]:
    """Group a flat measurement list into rounds, oldest first: two
    consecutive measurements belong to the same round while their
    timestamps are at most `max_gap_seconds` apart. Shared by
    `recent_rounds` and the evidence report -- one round definition,
    everywhere."""
    ascending = sorted(measurements, key=lambda m: m.timestamp)
    rounds: list[HistoryRound] = []
    current: HistoryRound | None = None
    gap = timedelta(seconds=max_gap_seconds)
    for m in ascending:
        if current is None or (m.timestamp - current.timestamp) > gap:
            current = HistoryRound(timestamp=m.timestamp)
            rounds.append(current)
        current.measurements.append(m)
    return rounds


def _score_rounds(rounds: list[HistoryRound], baseline: UserBaseline) -> None:
    """Score every round against `baseline`, in place. The re-computed
    score describes THAT round, stamped with its own time --
    score_measurements stamps utcnow() (its convention for live
    rounds), which would mislabel historical rows."""
    for round_ in rounds:
        round_.experience = score_measurements(round_.measurements, baseline)
        if round_.experience is not None:
            round_.experience.timestamp = round_.timestamp


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

    rounds = _group_rounds(raw, max_gap_seconds=max_gap_seconds)
    _score_rounds(rounds[-limit:], container.load_baseline())
    return rounds[-limit:]


# ---------------------------------------------------------------------------
# Evidence report (v0.5) -- `netscope report`: the persisted measurement
# log rebuilt into one honest, self-contained evidence document. The
# report only ever claims what the log proves: absent metrics render as
# absent, failures are listed verbatim, and round scores carry the same
# recomputed-against-the-current-baseline caveat as `netscope history`.
# ---------------------------------------------------------------------------

# Per-target table cap in the rendered document. The log can hold
# thousands of rows for a long window; the report shows the newest 40
# per target and *discloses* the omitted count instead of truncating
# silently -- an evidence document that hides evidence is not evidence.
REPORT_ROWS_PER_TARGET = 40
REPORT_MAX_ROUNDS = 40
REPORT_MAX_FAILURES = 20


@dataclass
class TargetReportRow:
    """One measurement as it appears in a target's evidence table.
    Carries its target name so the report-wide failure list can be
    rendered without re-deriving context.

    `geo` (v0.12) is the measurement's persisted `extra["geo"]` dict,
    verbatim -- the place/distance fact AS THE RUN RECORDED IT, never
    re-looked-up at report time: a report is what the log proves, and
    yesterday's lookup result does not silently become today's."""

    target: str
    timestamp: datetime
    probe_type: ProbeType
    success: bool
    latency_ms: float | None
    packet_loss_pct: float | None
    jitter_ms: float | None  # v0.13: the sweep's delay-variation reading, when the probe reports one
    error: str | None
    error_type: ProbeErrorType | None
    verdict: str | None  # v0.4 baseline-relative throughput verdict, when attached
    geo: dict | None = None


@dataclass
class TargetReport:
    """Per-target aggregates plus the (capped) evidence rows.

    `geo` (v0.12) is the target's most recent persisted geo block in
    the window -- the place/distance the NEWEST geo-aware measurement
    recorded, surfaced once per target in the geography sections; a
    target never geo-enriched keeps None, and the sections simply
    omit it."""

    target: str
    probe_types: list[str]
    rows: list[TargetReportRow]
    measurements: int
    failures: int
    mean_latency_ms: float | None
    verdict_counts: dict[str, int]
    truncated: int = 0
    geo: dict | None = None


@dataclass
class EvidenceReport:
    """The full evidence-report payload: window, rounds, per-target
    evidence and the honest totals. Serialization (JSON/markdown) lives
    in app.reports; this dataclass is the domain-side contract."""

    since: datetime
    until: datetime
    scope: str | None = None  # None = every target; a name = single-target export
    rounds: list[HistoryRound] = field(default_factory=list)
    target_reports: list[TargetReport] = field(default_factory=list)
    failure_rows: list[TargetReportRow] = field(default_factory=list)

    @property
    def targets(self) -> list[str]:
        return [t.target for t in self.target_reports]

    @property
    def measurements(self) -> int:
        return sum(t.measurements for t in self.target_reports)

    @property
    def failures(self) -> int:
        return sum(t.failures for t in self.target_reports)

    @property
    def mean_score(self) -> float | None:
        scores = [r.experience.score for r in self.rounds if r.experience is not None]
        return sum(scores) / len(scores) if scores else None


def _window_measurements(
    container: Container,
    *,
    since: datetime,
    until: datetime,
    target: str | None,
) -> list[RawMeasurement]:
    """Every persisted measurement inside the inclusive [since, until]
    window, oldest first -- fetched as one range query per target
    (`history()`), never one unbounded scan of the log. Without a wired
    measurement repository there is no evidence to report: an empty
    list, not an error."""
    repo = container.measurement_repository
    if repo is None:
        return []
    names = [target] if target is not None else repo.targets()
    pool: list[RawMeasurement] = []
    for name in names:
        pool.extend(repo.history(name, start=since, end=until, limit=None))
    pool.sort(key=lambda m: m.timestamp)
    return pool


def _report_row(m: RawMeasurement) -> TargetReportRow:
    """RawMeasurement -> evidence-table row. The v0.4 verdict rides
    along only when `extra["verdict"]` is actually a string -- a
    bandwidth row's verdict never gets to pose as latency evidence.
    The v0.12 geo block rides along only when it is actually a dict
    -- same shape-honesty rule."""
    raw_verdict = m.extra.get("verdict")
    raw_geo = m.extra.get("geo")
    return TargetReportRow(
        target=m.target,
        timestamp=m.timestamp,
        probe_type=m.probe_type,
        success=m.success,
        latency_ms=m.latency_ms,
        packet_loss_pct=m.packet_loss_pct,
        jitter_ms=m.jitter_ms,
        error=m.error,
        error_type=m.error_type,
        verdict=raw_verdict if isinstance(raw_verdict, str) else None,
        geo=raw_geo if isinstance(raw_geo, dict) else None,
    )


def _target_reports(measurements: list[RawMeasurement]) -> list[TargetReport]:
    """Group the window's measurements by target and aggregate each
    group. Latency means only ever include *successful* readings; the
    table keeps the NEWEST `REPORT_ROWS_PER_TARGET` rows (chronological
    within the slice) and discloses what it omitted -- verdict counts
    are always computed over the full window, cap or no cap."""
    by_target: dict[str, list[RawMeasurement]] = {}
    for m in measurements:
        by_target.setdefault(m.target, []).append(m)

    reports: list[TargetReport] = []
    for name in sorted(by_target):
        group = by_target[name]
        rows_all = [_report_row(m) for m in group]
        shown = rows_all[-REPORT_ROWS_PER_TARGET:]
        verdict_counts: dict[str, int] = {}
        for row in rows_all:
            if row.verdict is not None:
                verdict_counts[row.verdict] = verdict_counts.get(row.verdict, 0) + 1
        latencies = [m.latency_ms for m in group if m.success and m.latency_ms is not None]
        geo = next((row.geo for row in reversed(rows_all) if row.geo is not None), None)
        reports.append(
            TargetReport(
                target=name,
                probe_types=sorted({m.probe_type.value for m in group}),
                rows=shown,
                measurements=len(rows_all),
                failures=sum(1 for m in group if not m.success),
                mean_latency_ms=sum(latencies) / len(latencies) if latencies else None,
                verdict_counts=verdict_counts,
                truncated=len(rows_all) - len(shown),
                geo=geo,
            )
        )
    return reports


def build_evidence_report(
    container: Container,
    *,
    days: int = 7,
    target: str | None = None,
    until: datetime | None = None,
    max_gap_seconds: int = 120,
) -> EvidenceReport:
    """Build the v0.5 evidence report over the last `days` days of the
    persisted measurement log (optionally scoped to one `target`).

    The window is inclusive on both ends; `until` defaults to now and
    is injectable so tests (and reproducible exports) can pin it. Round
    scores are recomputed against the *current* personal baseline -- the
    same honest approximation `recent_rounds` makes, since the log alone
    cannot reconstruct the baseline as it stood at each round's time."""
    if days <= 0:
        raise ValueError(f"days must be a positive integer, got {days!r}")
    end = until if until is not None else utcnow()
    since = end - timedelta(days=days)

    pool = _window_measurements(container, since=since, until=end, target=target)
    rounds = _group_rounds(pool, max_gap_seconds=max_gap_seconds)
    _score_rounds(rounds, container.load_baseline())

    return EvidenceReport(
        since=since,
        until=end,
        scope=target,
        rounds=rounds,
        target_reports=_target_reports(pool),
        failure_rows=sorted((r for r in (_report_row(m) for m in pool) if not r.success), key=lambda r: r.timestamp),
    )
