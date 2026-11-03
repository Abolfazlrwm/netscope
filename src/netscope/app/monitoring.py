"""
netscope.app.monitoring

The monitoring loop's application-level state machine: wraps
`core.incidents.detect_incident` (until now fully implemented and
tested but wired to nothing) into a session the `netscope monitor`
command and TUI can drive round after round.

WHY THIS LIVES IN app
---------------------
`core/incidents.py` is deliberately stateless and wall-clock-free
(module-boundaries.md: it "must not persist itself" and "must not run
measurements itself") -- *somebody* has to keep the rolling window of
rounds and the currently-open incident between rounds, and that
somebody is the application layer. This module is that somebody: it
accumulates `MonitoringRound`s, asks `detect_incident` what to do, and
reports the transition (opened / updated / closed / none) -- no
measurement, classification, or severity logic of its own.

TRANSITION SEMANTICS
--------------------
`process()` compares the incident state before and after
`detect_incident` ran, so the CLI can print exactly one honest line per
round instead of re-deriving the state machine's internals:
- opened:  no incident existed, a confirmed problem became sustained
- updated: an active incident stays active (evidence accumulates)
- closed:  an active incident ended (sustained healthy rounds)
- none:    no change worth reporting (including sustained
           INSUFFICIENT_EVIDENCE stretches, which by design never
           open or close anything)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from netscope.core.incidents import MonitoringRound, detect_incident
from netscope.core.models import Diagnosis, Incident, utcnow

# Rolling window kept per session. detect_incident only needs the
# trailing run sharing the last round's status, so a bounded window
# keeps long monitor sessions at constant memory without changing any
# detection outcome.
_MAX_ROUNDS = 50


class TransitionType(str, Enum):
    OPENED = "opened"
    UPDATED = "updated"
    CLOSED = "closed"
    NONE = "none"


@dataclass(frozen=True)
class IncidentTransition:
    """What happened to incident state as a result of one processed
    round -- `incident` is the (possibly new/updated/closed) Incident,
    or None when there has never been one."""

    kind: TransitionType
    incident: Incident | None

    @property
    def is_significant(self) -> bool:
        """True for transitions a monitor UI should surface
        (opened/closed/updated) as opposed to a quiet round."""
        return self.kind is not TransitionType.NONE


class MonitorSession:
    """Per-target monitoring state for one `netscope monitor` run."""

    def __init__(self, target: str | None = None, sustain_threshold: int = 3):
        self.target = target
        self.sustain_threshold = sustain_threshold
        self.rounds: list[MonitoringRound] = []
        self.incident: Incident | None = None
        self.round_count = 0

    def process(self, diagnosis: Diagnosis | None, timestamp: datetime | None = None) -> IncidentTransition:
        """Feed one round's Diagnosis (or None = healthy) into the
        session, return the incident transition it produced."""
        # Timestamp resolution: the caller's explicit stamp wins; else
        # the Diagnosis' own (the healthy-round gap core/incidents.py
        # documents); else the wall clock -- app layer, unlike core, is
        # allowed to read it.
        if timestamp is None:
            timestamp = diagnosis.timestamp if diagnosis is not None else utcnow()
        self.round_count += 1
        self.rounds.append(MonitoringRound(timestamp=timestamp, diagnosis=diagnosis))
        self.rounds = self.rounds[-_MAX_ROUNDS:]

        previous = self.incident
        new_incident = detect_incident(
            self.rounds,
            self.incident,
            target=self.target,
            sustain_threshold=self.sustain_threshold,
        )
        self.incident = new_incident

        return IncidentTransition(kind=self._classify(previous, new_incident), incident=new_incident)

    @staticmethod
    def _classify(previous: Incident | None, current: Incident | None) -> TransitionType:
        # detect_incident returns the SAME object when nothing changed
        # (including sustained INSUFFICIENT_EVIDENCE stretches) -- identity,
        # not equality, distinguishes "unchanged" from a genuinely
        # new/updated/closed Incident (which are always fresh objects).
        if current is None or current is previous:
            return TransitionType.NONE
        if previous is None:
            return TransitionType.OPENED
        if previous.is_active and current.is_active:
            return TransitionType.UPDATED
        if previous.is_active and not current.is_active:
            return TransitionType.CLOSED
        return TransitionType.NONE
