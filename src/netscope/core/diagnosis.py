"""
netscope.core.diagnosis

Evidence/hypothesis-based diagnosis engine (TASK-026), replacing
`diagnosis/engine.py`'s fixed three-way `if`/`elif` chain, per
`docs/architecture/architecture-overview.md` SS11 and
`docs/architecture/module-boundaries.md`'s Diagnosis section --
extended by TASK-027 ("Evidence generation") to add the
measurement/route/baseline -> Evidence transformation functions this
file's `diagnose()` deliberately does not do itself.

WHY THIS LIVES IN core
------------------------
Pure domain computation: no I/O, no adapters, no persistence, no UI.

WHY EVIDENCE GENERATION IS IN *THIS* FILE, NOT A NEW core/evidence.py
------------------------------------------------------------------------
future-roadmap.md's TASK-027 row scopes itself explicitly as
"`core/diagnosis.py`, refactored" / "same file, restructured", and
module-boundaries.md's Diagnosis section says this module "Lives in:
core/diagnosis.py" with no separate Evidence-generation module named
anywhere in the architecture docs. Its "must not collect evidence and
select a cause in the same function" rule is a rule about function
boundaries (separately callable, separately testable), not file
boundaries -- so the evidence-generation functions below and the
cause-selection `diagnose()`/`_classify()` above satisfy it by being
entirely independent top-level functions that never call each other,
while staying in the one file the docs specify.

One consequence: the evidence-generation functions below need
`netscope.core.baseline` (to consult `UserBaseline`/`MetricBaseline`)
and `netscope.core.routing` (to consume `RouteChurnResult`), which
module-boundaries.md's original "Dependencies: core.models only" line
predates -- that line described only the cause-selection half this
file had before TASK-027. Both new imports are still `netscope.core.*`
siblings, never leaving core, and deliberately NOT
`netscope.core.scoring`: evidence generation and scoring are
independent consumers of the same baseline, not dependent on each
other, per adr-001-architecture-style.md's general dependency-direction
rule (which core/ports.py, core/scoring.py, and this file's original
TASK-026 docstring above already document for themselves).

THE UNTESTED-GATEWAY BUG, AND THE FIX
-----------------------------------------
The old diagnosis/engine.py's `is_bad(m: RawMeasurement | None) -> bool`
returned `False` for `m is None` -- collapsing "confirmed healthy" and
"never measured" into the exact same boolean. Because the local-gateway
branch was checked first and just asked `if not gw_bad`, an untested
gateway silently unlocked every downstream branch exactly as if it had
been positively tested and found healthy (see the retired
`test_untested_gateway_currently_behaves_as_healthy` test in
tests/test_diagnosis.py, which is rewritten by this task to assert the
corrected behavior instead of documenting the bug).

This module never asks a boolean "is it bad" question about a metric
category at all. It always asks two independent questions about each
category: "is there evidence this was actually tested?" and, only if
so, "does the tested evidence show a problem?". A gateway that was
never tested therefore can never satisfy "confirmed healthy" -- it can
only ever be *unknown*, and unknown local-network status is exactly
what routes a diagnosis to `Hypothesis.INSUFFICIENT_EVIDENCE` instead
of confidently attributing a problem upstream (see `_classify` below,
the `gateway_untested` branch). TASK-027's evidence-generation
functions extend the same principle one layer earlier: a `None`
measurement/churn-result input always produces `tested=False` Evidence,
never a fabricated healthy or failed reading.
"""

from __future__ import annotations

from netscope.core.baseline import MetricBaseline, UserBaseline
from netscope.core.models import Diagnosis, Evidence, Hypothesis, RawMeasurement, RouteSnapshot, Severity
from netscope.core.routing import RouteChurnResult

# ---------------------------------------------------------------------------
# Metric -> Hypothesis category vocabulary
# ---------------------------------------------------------------------------
#
# Evidence.metric is a plain string (core/models.py's TASK-009 design,
# e.g. "gateway_latency") -- there is no existing controlled vocabulary
# for it anywhere in the repository yet. This module establishes the
# minimal one it needs to localize a problem: the prefix before the
# first underscore identifies *what* was measured (gateway, DNS,
# routing, destination/CDN, or an explicitly-labeled Service). Metrics
# with an unrecognized or absent prefix (e.g. the "latency" example in
# this task's own brief) are simply uncategorized -- they still count
# as evidence of *a* problem, just not one this module can localize to
# a specific Hypothesis, so they fall through to
# Hypothesis.GENERAL_CONNECTIVITY_ISSUE rather than being silently
# dropped or guessed at. TASK-027 (evidence generation) is the natural
# place to extend or formalize this vocabulary once it owns actually
# producing Evidence from real measurements.
_METRIC_CATEGORY: dict[str, Hypothesis] = {
    "gateway": Hypothesis.LOCAL_NETWORK_ISSUE,
    "dns": Hypothesis.DNS_ISSUE,
    "route": Hypothesis.ROUTING_DEGRADATION,
    "routing": Hypothesis.ROUTING_DEGRADATION,
    "destination": Hypothesis.DESTINATION_ISSUE,
    "cdn": Hypothesis.DESTINATION_ISSUE,
    "service": Hypothesis.SERVICE_ISSUE,
    "isp": Hypothesis.ISP_ACCESS_ISSUE,
}


def _category(metric: str) -> Hypothesis | None:
    prefix = metric.split("_", 1)[0].lower()
    return _METRIC_CATEGORY.get(prefix)


def _is_problem(e: Evidence) -> bool:
    return e.tested and e.severity in (Severity.WARNING, Severity.CRITICAL)


def _is_confirmed_healthy(e: Evidence) -> bool:
    return e.tested and e.severity == Severity.INFO


def _confidence(supporting: list[Evidence], all_evidence: list[Evidence]) -> float:
    """Confidence is derived from evidence quality, never a hand-picked
    per-branch constant (architecture-overview.md SS11's explicit
    complaint about today's 80.0/70.0/65.0/90.0). Two independent
    factors, each already present on Evidence/derivable from it:

    - `strength`: the average `confidence` the *supporting* Evidence
      items themselves carry (multiple agreeing, high-confidence
      signals push this up; a single weak one keeps it modest).
    - `tested_fraction`: how much of the *total* evidence considered
      was actually tested (untested items can neither confirm nor deny
      anything, so more of them mean less complete a picture, and
      confidence is scaled down accordingly -- this is what "missing
      tests -> low confidence" means concretely).

    Multiplying keeps the result in [0, 1] without any new hand-picked
    constant, and rounds to 2 decimal places purely for readability.
    """
    if not supporting or not all_evidence:
        return 0.0
    strength = sum(e.confidence for e in supporting) / len(supporting)
    tested_fraction = sum(1 for e in all_evidence if e.tested) / len(all_evidence)
    return round(strength * tested_fraction, 2)


def _classify(evidence: list[Evidence]) -> tuple[Hypothesis, list[Evidence], list[Evidence | Hypothesis]]:
    """Returns (classification, supporting_evidence, ruled_out).
    Only called when `evidence` is non-empty."""

    tested = [e for e in evidence if e.tested]
    untested = [e for e in evidence if not e.tested]
    problems = [e for e in tested if _is_problem(e)]
    healthy = [e for e in tested if _is_confirmed_healthy(e)]

    local_problem = any(_category(e.metric) == Hypothesis.LOCAL_NETWORK_ISSUE for e in problems)
    gateway_untested = any(_category(e.metric) == Hypothesis.LOCAL_NETWORK_ISSUE for e in untested)
    gateway_confirmed_healthy = any(
        _category(e.metric) == Hypothesis.LOCAL_NETWORK_ISSUE for e in healthy
    )

    if not problems:
        if untested:
            # Nothing tested shows a problem, but not everything relevant
            # was tested either -- "missing != successful" (this is the
            # symmetric case of the untested-gateway bug: an untested
            # DNS/CDN target must not silently read as healthy any more
            # than an untested gateway may). Nothing was actually ruled
            # out here -- the untested items are the reason we can't
            # rule anything out -- they're already visible via the
            # Diagnosis.evidence field, not stuffed into ruled_out.
            return Hypothesis.INSUFFICIENT_EVIDENCE, [], []
        # Everything considered was actually tested, and none of it
        # showed a problem -- a genuinely confirmed-healthy result.
        # Callers see this as diagnose() returning None (see below);
        # _classify itself is never asked to produce a "no issue"
        # Hypothesis, because none exists in the canonical enum.
        raise AssertionError("_classify must not be called with no problem and no untested evidence")

    if local_problem:
        # Mirrors the old engine's "gateway bad takes priority" rule
        # (test_diagnosis_bad_gateway_takes_priority_even_if_everything_
        # else_is_also_bad), which module-boundaries.md/implementation-
        # audit.md both say to preserve as a *concept* even though the
        # implementation changes: if the local hop itself demonstrably
        # fails, that's evidence enough regardless of what else looks
        # bad upstream (an upstream target can look bad *because* the
        # local link to reach it is broken).
        supporting = [e for e in problems if _category(e.metric) == Hypothesis.LOCAL_NETWORK_ISSUE]
        return Hypothesis.LOCAL_NETWORK_ISSUE, supporting, []

    if gateway_untested:
        # THE BUG FIX: the local hop was never tested, so we cannot
        # confirm it's healthy -- and therefore cannot confidently
        # attribute the observed problem(s) upstream either, no matter
        # how bad DNS/CDN/etc. look. This is exactly the scenario the
        # old is_bad(None) == False silently resolved to "gateway is
        # fine" and then confidently blamed the ISP. Here it resolves
        # to INSUFFICIENT_EVIDENCE, and -- critically -- LOCAL_NETWORK_
        # ISSUE never appears in ruled_out, because it genuinely wasn't.
        return Hypothesis.INSUFFICIENT_EVIDENCE, problems, []

    # From here on, the gateway is either confirmed healthy, or there
    # was no gateway-category evidence at all (neither tested nor
    # untested -- e.g. a caller that only ever measures DNS/CDN). Both
    # are safe to attribute upstream: we're not claiming the gateway IS
    # healthy in the second case, only that we have no evidence it's
    # the cause, which is a materially weaker (and honest) claim.
    categories = {c for e in problems if (c := _category(e.metric)) is not None and c != Hypothesis.LOCAL_NETWORK_ISSUE}
    uncategorized = [e for e in problems if _category(e.metric) is None]

    ruled_out: list[Evidence | Hypothesis] = list(healthy)
    if gateway_confirmed_healthy:
        ruled_out.append(Hypothesis.LOCAL_NETWORK_ISSUE)

    if len(categories) >= 2 or (len(categories) == 1 and uncategorized):
        # Independent problem categories (e.g. DNS *and* destination
        # both bad) is the old engine's "upstream ISP issue" signal --
        # multiple, unrelated targets failing together points beyond
        # any one of them individually.
        return Hypothesis.ISP_ACCESS_ISSUE, problems, ruled_out
    if len(categories) == 1:
        (only,) = categories
        supporting = [e for e in problems if _category(e.metric) == only]
        return only, supporting, ruled_out
    # Problem evidence exists but its metric name doesn't map to any
    # known category (e.g. this task's own brief's bare "latency"
    # example) -- there is a real problem, just not one this module can
    # localize, so it is reported rather than guessed at.
    return Hypothesis.GENERAL_CONNECTIVITY_ISSUE, problems, ruled_out


def diagnose(evidence: list[Evidence]) -> Diagnosis | None:
    """Convert a list of Evidence into a Diagnosis, or None if the
    evidence confirms a healthy state with nothing to diagnose.

    `None` (not a Diagnosis with some "no issue" classification) is the
    healthy result on purpose: `Hypothesis` (core/models.py, verbatim
    from architecture-overview.md SS11) has no "no issue" member --
    intentionally, since fabricating one alongside the other 7 problem
    classifications would blur exactly the line this task exists to
    sharpen. A Diagnosis conceptually represents *a diagnosed problem*
    (module-boundaries.md); when there isn't one, there is nothing to
    diagnose, mirroring how `core/incidents.py` (TASK-028) will
    eventually only exist for sequences of actual Diagnoses, not for
    quiet periods.

    Requires ALL of `evidence` to be tested and healthy to return
    `None` -- any untested item, even alongside otherwise-healthy
    tested evidence, means the picture is incomplete, and this
    correctly returns Hypothesis.INSUFFICIENT_EVIDENCE instead of a
    silent healthy result. This is deliberately symmetric with the
    untested-gateway fix: "missing != successful" applies to every
    metric category, not just the gateway.
    """
    if not evidence:
        return Diagnosis(classification=Hypothesis.INSUFFICIENT_EVIDENCE, evidence=[], confidence=0.0, ruled_out=[])

    tested = [e for e in evidence if e.tested]
    untested = [e for e in evidence if not e.tested]
    problems = [e for e in tested if _is_problem(e)]

    if not problems and not untested:
        return None

    classification, supporting, ruled_out = _classify(evidence)
    confidence = 0.0 if classification is Hypothesis.INSUFFICIENT_EVIDENCE else _confidence(supporting, evidence)

    return Diagnosis(
        classification=classification,
        evidence=evidence,
        confidence=confidence,
        ruled_out=ruled_out,
    )


# =============================================================================
# Evidence generation (TASK-027)
# =============================================================================
#
# The functions below turn a RawMeasurement, a RouteChurnResult, or their
# absence (None) into Evidence -- the transformation `diagnose()` above
# deliberately never does itself. None of these functions call diagnose()
# or _classify(), and diagnose()/_classify() never call these -- the two
# halves are independently callable and independently tested, per
# module-boundaries.md's "must not collect evidence and select a cause
# in the same function" rule.
#
# Neither this section nor diagnose() ever mutates the UserBaseline it's
# given: only MetricBaseline's already-read-only surface is used
# (`.count`, `.mean`, `.stddev`, `.deviation_sigma()`) via plain dict
# lookups (`baseline.latency.get(target)`), exactly mirroring how
# core/scoring.py (TASK-025) reads a baseline without recording new
# observations into it. Baseline learning/persistence stays entirely
# outside this module, as it does for scoring.

# Neither of the two numbers below is a new arbitrary threshold. Both are
# read directly from decisions netscope.core.baseline has already made
# (and TASK-025 already reused this same way for the analogous scoring
# problem): MetricBaseline.deviation_sigma() itself refuses to produce a
# real signal below 5 samples, and MetricBaseline.is_anomalous()'s own
# default sigma_threshold is 2.5.
_MATURE_SAMPLE_COUNT = 5
_ANOMALY_SIGMA_THRESHOLD = 2.5


def _evidence_from_metric_baseline(
    metric: str,
    target: str,
    value: float | None,
    store: dict[str, MetricBaseline],
    source: RawMeasurement | RouteSnapshot | None,
) -> Evidence:
    """Shared baseline-comparison logic for both evidence_from_latency
    and evidence_from_packet_loss below -- reads `store` (a UserBaseline's
    `.latency` or `.packet_loss` dict) read-only via `.get(target)`,
    never `UserBaseline`'s mutating `observe_*`/`_get`. Per-target
    isolation falls out of this for free: `store.get(target)` can only
    ever return that target's own MetricBaseline (or None), never
    another target's.
    """
    if value is None:
        # The measurement succeeded but didn't report this particular
        # field (e.g. a probe type with no packet-loss figure) -- tested,
        # but nothing to compare against a baseline; zero confidence so
        # it can't be mistaken for either a healthy or a bad reading.
        return Evidence(metric=metric, tested=True, source=source, confidence=0.0)

    baseline_metric = store.get(target)
    if baseline_metric is None or baseline_metric.count < _MATURE_SAMPLE_COUNT:
        # Insufficient history: distinct from "value matches the mean"
        # (core/scoring.py's TASK-025 precedent for this exact
        # distinction). Confidence scales with how much history exists,
        # 0.0 with none up to full at _MATURE_SAMPLE_COUNT; no claim is
        # made about whether `value` is normal or abnormal.
        count = baseline_metric.count if baseline_metric is not None else 0
        confidence = min(count / _MATURE_SAMPLE_COUNT, 1.0)
        return Evidence(metric=metric, tested=True, observed_value=value, source=source, severity=Severity.INFO, confidence=confidence)

    if baseline_metric.stddev == 0.0:
        # Mature but has never varied -- deviation_sigma() is defined to
        # return 0.0 here too (division-by-zero guard), which would be
        # indistinguishable from "value == mean" if relied on directly
        # (same TASK-025 precedent). Checked explicitly instead.
        if value == baseline_metric.mean:
            return Evidence(
                metric=metric, tested=True, observed_value=value, expected_value=baseline_metric.mean,
                deviation=0.0, source=source, severity=Severity.INFO, confidence=1.0,
            )
        return Evidence(
            metric=metric, tested=True, observed_value=value, expected_value=baseline_metric.mean,
            deviation=None, source=source, severity=Severity.CRITICAL, confidence=1.0,
        )

    sigma = baseline_metric.deviation_sigma(value)
    if sigma <= 0:
        severity = Severity.INFO
    elif sigma < _ANOMALY_SIGMA_THRESHOLD:
        severity = Severity.WARNING
    else:
        severity = Severity.CRITICAL
    return Evidence(
        metric=metric, tested=True, observed_value=value, expected_value=baseline_metric.mean,
        deviation=sigma, source=source, severity=severity, confidence=1.0,
    )


def evidence_from_latency(metric: str, measurement: RawMeasurement | None, baseline: UserBaseline) -> Evidence:
    """RawMeasurement (or None) + baseline -> latency Evidence.

    `measurement=None` means this target was never probed at all --
    `tested=False`, never a fabricated healthy/bad reading (this is the
    evidence-generation-layer instance of the untested-gateway fix: it
    applies to every metric produced here, not only a literal gateway).
    A `measurement` with `success=False` is unambiguous CRITICAL
    evidence of its own -- it does not need, and does not consult, the
    baseline at all (a failed probe has no latency to compare).
    """
    if measurement is None:
        return Evidence(metric=metric, tested=False)
    if not measurement.success:
        return Evidence(metric=metric, tested=True, source=measurement, severity=Severity.CRITICAL, confidence=1.0)
    return _evidence_from_metric_baseline(metric, measurement.target, measurement.latency_ms, baseline.latency, measurement)


def evidence_from_packet_loss(metric: str, measurement: RawMeasurement | None, baseline: UserBaseline) -> Evidence:
    """Same contract as evidence_from_latency, for packet-loss."""
    if measurement is None:
        return Evidence(metric=metric, tested=False)
    if not measurement.success:
        return Evidence(metric=metric, tested=True, source=measurement, severity=Severity.CRITICAL, confidence=1.0)
    return _evidence_from_metric_baseline(metric, measurement.target, measurement.packet_loss_pct, baseline.packet_loss, measurement)


def evidence_from_route_churn(
    metric: str,
    churn: RouteChurnResult | None,
    latest_snapshot: RouteSnapshot | None = None,
) -> Evidence:
    """RouteChurnResult (core.routing.analyze_route_churn's own output --
    not reimplemented here, per this task's "do not build a second
    routing analysis system" scope) -> route-stability Evidence.

    `churn=None` means route stability was never actually assessed (no
    traceroute history exists yet to analyze) -- `tested=False`, exactly
    like an unprobed target, never a fabricated "stable" reading.

    Route churn has no baseline concept of its own (TASK-021's
    `core.routing` is structural signature-change analysis only, with no
    UserBaseline integration) -- so unlike latency/packet-loss, severity
    here is derived directly from `change_count` rather than a sigma,
    since there is no learned "normal churn rate" to compare against.
    `latest_snapshot`, if the caller has it, is preserved as `source` so
    this Evidence can point back to an actual RouteSnapshot -- fully
    optional, since a RouteChurnResult alone doesn't carry one.
    """
    if churn is None:
        return Evidence(metric=metric, tested=False)
    if churn.is_stable:
        return Evidence(metric=metric, tested=True, observed_value=0.0, expected_value=0.0, deviation=0.0, source=latest_snapshot, severity=Severity.INFO, confidence=1.0)
    severity = Severity.WARNING if churn.change_count == 1 else Severity.CRITICAL
    return Evidence(
        metric=metric,
        tested=True,
        observed_value=float(churn.change_count),
        expected_value=0.0,
        deviation=float(churn.change_count),
        source=latest_snapshot,
        severity=severity,
        confidence=1.0,
    )
