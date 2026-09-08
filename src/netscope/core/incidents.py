"""
netscope.core.incidents

Incident detection (TASK-028), per
`docs/architecture/module-boundaries.md`'s Monitoring (Incident
Detection) section and Phase 1 research's Differentiator #5:
incidents require multiple corroborating signals over time, not one
bad ping, unlike classic threshold-alerting tools.

WHY THIS LIVES IN core
------------------------
Pure domain computation: no I/O, no adapters, no persistence, no UI,
no wall-clock reads. Per module-boundaries.md ("Dependencies:
core.models only"), the only import is netscope.core.models and the
Python standard library (datetime, for typing only -- see
MonitoringRound below).

WHAT THIS MODULE DOES *NOT* DO
---------------------------------
- Does not run measurements, generate Evidence, or produce a Diagnosis
  -- it only consumes Diagnosis results a caller ("app", not yet built)
  hands it after each round (module-boundaries.md: "Must not run
  measurements itself").
- Does not decide *what* the problem is -- that classification already
  came from the Diagnosis it receives; this module only decides
  *whether a diagnosis is sustained enough to become an Incident*
  (module-boundaries.md: "Must not decide what the problem is").
- Does not persist itself -- returns an Incident (or None); a future
  `app` layer persists it via a repository port, same pattern as
  Diagnosis/Intelligence (module-boundaries.md: "Must not persist
  itself directly").

THE SUSTAINED-SIGNAL REQUIREMENT
------------------------------------
module-boundaries.md's own testing strategy gives the exact behavior
to implement: "3 consecutive ISP_ACCESS_ISSUE diagnoses 5 minutes
apart -> Incident opens; 1 bad diagnosis surrounded by healthy ones ->
no Incident." `detect_incident` below implements exactly this: it
looks at the trailing run of consecutive rounds sharing the same
status (a specific Hypothesis, "healthy", or INSUFFICIENT_EVIDENCE) at
the end of the given history, and only acts once that run reaches
`sustain_threshold` (default 3, matching the doc's own example). A
single noisy sample -- whether a bad diagnosis or a single healthy
round in the middle of an active incident -- never opens, closes, or
otherwise disturbs incident state.

WHY MonitoringRound EXISTS
-----------------------------
"Timestamps are passed in, not read from the wall clock, inside core"
(module-boundaries.md's Testing strategy) rules out `datetime.now()`
anywhere in this module. `Diagnosis` already carries its own
`timestamp`, but a healthy round (no problem detected) has no
Diagnosis object at all to carry one -- so closing a sustained-healthy
incident needs a timestamp from somewhere. `MonitoringRound` is the
minimal fix: it pairs an explicit `timestamp` with that round's
`Diagnosis | None`. It is deliberately NOT a new competing domain
model alongside Diagnosis/Incident/Evidence -- it carries no
diagnostic information of its own, only the one piece of context
(when) a bare `None` can't supply on its own.

THE UNTESTED-EVIDENCE PRINCIPLE, PRESERVED ONE LAYER UP
------------------------------------------------------------
This module never inspects `Evidence.tested` directly -- that
distinction was already resolved by `core.diagnosis.diagnose()`, which
returns `Hypothesis.INSUFFICIENT_EVIDENCE` (not `None`, not a fabricated
problem classification) exactly when evidence was missing/untested and
therefore inconclusive. `detect_incident` treats
`INSUFFICIENT_EVIDENCE` as its own third status, distinct from both
"healthy" (`None`) and a confirmed problem: it can never open a new
incident (see requirement 2), and it never closes or otherwise disturbs
an already-open one either, since a stretch of "we don't know" is not
evidence that a real problem resolved. This is the same "missing !=
successful, missing != failed" principle TASK-026/027 established,
carried one layer up to incident lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from netscope.core.models import Diagnosis, Evidence, Hypothesis, Incident, Severity

_HEALTHY = "healthy"  # sentinel status for a round with diagnosis=None


@dataclass(frozen=True)
class MonitoringRound:
    """One round of diagnosis over time: a timestamp plus that round's
    Diagnosis, or None if nothing was diagnosed (a healthy round). See
    this module's docstring for why the timestamp can't simply be read
    off the Diagnosis itself in every case.
    """

    timestamp: datetime
    diagnosis: Diagnosis | None


def _status(round_: MonitoringRound) -> Hypothesis | str:
    return _HEALTHY if round_.diagnosis is None else round_.diagnosis.classification


def _trailing_run(rounds: list[MonitoringRound]) -> tuple[Hypothesis | str, list[MonitoringRound]]:
    """The status of the most recent round, and the contiguous run of
    rounds at the end of `rounds` sharing that same status."""
    last_status = _status(rounds[-1])
    run: list[MonitoringRound] = []
    for round_ in reversed(rounds):
        if _status(round_) != last_status:
            break
        run.append(round_)
    run.reverse()
    return last_status, run


def _union_evidence(existing: list[Evidence], new: list[Evidence]) -> list[Evidence]:
    """Evidence across the incident's lifetime, not just its start
    (core/models.py's Incident docstring, TASK-009) -- appends only
    genuinely new items, preserving order, without duplicating an
    Evidence object already present. Never invents an item that isn't
    already one of the canonical structured Evidence objects a real
    Diagnosis carried.
    """
    merged = list(existing)
    for item in new:
        if item not in merged:
            merged.append(item)
    return merged


def _severity_from_evidence(evidence: list[Evidence]) -> Severity:
    """Derived entirely from the actual supporting Evidence's own
    severities -- never an arbitrary static latency/loss threshold
    (those were intentionally retired by TASK-025/026). CRITICAL if any
    supporting evidence item is CRITICAL, else WARNING if any is
    WARNING, else INFO.
    """
    severities = {e.severity for e in evidence}
    if Severity.CRITICAL in severities:
        return Severity.CRITICAL
    if Severity.WARNING in severities:
        return Severity.WARNING
    return Severity.INFO


def _open(sustained_run: list[MonitoringRound], target: str | None) -> Incident:
    first_diagnosis = sustained_run[0].diagnosis
    latest_diagnosis = sustained_run[-1].diagnosis
    assert first_diagnosis is not None and latest_diagnosis is not None  # guaranteed by caller (status != _HEALTHY)
    evidence = _union_evidence([], latest_diagnosis.evidence)
    return Incident(
        started_at=sustained_run[0].timestamp,
        ended_at=None,
        severity=_severity_from_evidence(evidence),
        target=target,
        evidence=evidence,
        diagnosis=latest_diagnosis,
    )


def _update(existing: Incident, sustained_run: list[MonitoringRound], target: str | None) -> Incident:
    latest_diagnosis = sustained_run[-1].diagnosis
    assert latest_diagnosis is not None
    evidence = _union_evidence(existing.evidence, latest_diagnosis.evidence)
    return Incident(
        started_at=existing.started_at,  # an update, not a new incident -- start time never moves
        ended_at=None,
        severity=_severity_from_evidence(evidence),
        target=existing.target if existing.target is not None else target,
        evidence=evidence,
        diagnosis=latest_diagnosis,
    )


def _close(existing: Incident, ended_at: datetime) -> Incident:
    return Incident(
        started_at=existing.started_at,
        ended_at=ended_at,
        severity=existing.severity,
        target=existing.target,
        evidence=existing.evidence,
        diagnosis=existing.diagnosis,
    )


def detect_incident(
    rounds: list[MonitoringRound],
    existing_incident: Incident | None = None,
    *,
    target: str | None = None,
    sustain_threshold: int = 3,
) -> Incident | None:
    """Given the most recent monitoring rounds for one target (oldest
    first) and the currently-open Incident for that target (if any),
    decide whether to open, update, close, or leave incident state
    unchanged.

    `rounds` should be the recent rolling window for this target --
    maintaining that window across calls is the caller's job (a future
    `app` layer), not this module's; per module-boundaries.md this
    module "must not persist itself directly" and has no state of its
    own beyond what's passed in on each call.

    `sustain_threshold` defaults to 3, matching module-boundaries.md's
    own worked example ("3 consecutive ISP_ACCESS_ISSUE diagnoses 5
    minutes apart -> Incident opens").

    Returns the (possibly new/updated/closed) Incident, or None if there
    is not, and never was, one to report.
    """
    if not rounds:
        return existing_incident

    status, sustained_run = _trailing_run(rounds)

    if len(sustained_run) < sustain_threshold:
        # A single noisy sample -- in either direction -- must not open,
        # close, or otherwise disturb incident state. This is the literal
        # "1 bad diagnosis surrounded by healthy ones -> no Incident"
        # case, and its symmetric counterpart for a single healthy blip
        # inside an otherwise-ongoing incident.
        return existing_incident

    if status == _HEALTHY:
        if existing_incident is not None and existing_incident.is_active:
            return _close(existing_incident, sustained_run[0].timestamp)
        return existing_incident

    if status == Hypothesis.INSUFFICIENT_EVIDENCE:
        # Requirement: insufficient evidence must never be silently
        # converted into a confirmed incident -- and, symmetrically, a
        # sustained stretch of "we don't know" is not positive evidence
        # that an already-open incident has resolved, so it must not
        # close one either. State is left exactly as it was.
        return existing_incident

    # status is a confirmed problem Hypothesis, sustained for at least
    # sustain_threshold consecutive rounds.
    if existing_incident is not None and existing_incident.is_active:
        return _update(existing_incident, sustained_run, target)
    return _open(sustained_run, target)
