"""
netscope.core.diagnosis

Evidence/hypothesis-based diagnosis engine (TASK-026), replacing
`diagnosis/engine.py`'s fixed three-way `if`/`elif` chain, per
`docs/architecture/architecture-overview.md` SS11 and
`docs/architecture/module-boundaries.md`'s Diagnosis section.

WHY THIS LIVES IN core
------------------------
Pure domain computation: no I/O, no adapters, no persistence, no UI. The
only imports are the Python standard library and netscope.core.models,
per adr-001-architecture-style.md and the same dependency-direction
rule core/ports.py and core/scoring.py already document for themselves.

WHAT THIS MODULE DOES *NOT* DO
---------------------------------
module-boundaries.md's Diagnosis section explicitly forbids collecting
evidence and selecting a cause in the same function. This module never
reads a RawMeasurement, RouteSnapshot, or UserBaseline directly -- its
only input is an already-built `list[Evidence]` (core.models.Evidence).
Converting raw measurements into Evidence is a separate concern left
for a future task (TASK-027, "Evidence generation", explicitly owns
formalizing that as its own testable unit) -- this keeps
diagnose() a pure evidence-to-Diagnosis function from the start, rather
than something TASK-027 has to un-conflate later.

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
the `gateway_untested` branch).
"""

from __future__ import annotations

from netscope.core.models import Diagnosis, Evidence, Hypothesis, Severity

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
