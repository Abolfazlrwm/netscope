"""
netscope.core.routing

Route-stability analysis over a sequence of RouteSnapshots for one
target, ordered by time (TASK-021, "Route analysis").

Per module-boundaries.md's Routing section, this module only ANALYZES
RouteSnapshots it's given -- it never runs traceroute itself (that's
adapters/probes/traceroute_adapter.py's job) and never decides what a
detected change MEANS for diagnosis (that's a future core.diagnosis
concern, not yet built) -- it only reports what happened, as
evidence-shaped output for that future layer to interpret.

Route identity is judged purely by RouteSnapshot.signature() (already
implemented, TASK-003's tests) -- a fingerprint of ordered hop
addresses only, explicitly NOT of lookup metadata (asn/organization/
country, TASK-020's fields), confirmed unaffected by
tests/test_models.py::test_route_snapshot_signature_is_unaffected_by_lookup_fields.
This means route-churn detection here is automatically immune to a
future ASN/GeoIP lookup (TASK-022/023) being refreshed independently
of the actual path -- a lookup update alone can never register as a
route change.

This module has no I/O and imports nothing beyond core.models and the
Python standard library, per module-boundaries.md's stated dependency
constraint for Routing ("core.models only. No third-party library, no
I/O").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from netscope.core.models import RouteSnapshot


@dataclass
class RouteChange:
    """One detected transition between two consecutive RouteSnapshots
    for the same target -- their signature() differed."""

    previous_signature: str
    new_signature: str
    previous_timestamp: datetime
    new_timestamp: datetime


@dataclass
class RouteChurnResult:
    """Route-stability evidence derived from a sequence of
    RouteSnapshots for one target, ordered by time.

    This is evidence-shaped output for a future Diagnosis layer to
    interpret -- this dataclass does not decide what a given churn
    count *means* (e.g. whether it indicates a problem), only reports
    what was observed, per Routing's "must not decide the final
    diagnosis classification" responsibility.
    """

    target: str
    snapshot_count: int
    unique_signature_count: int
    changes: list[RouteChange] = field(default_factory=list)

    @property
    def change_count(self) -> int:
        return len(self.changes)

    @property
    def is_stable(self) -> bool:
        """True if no change was detected across the observed
        snapshots. A single snapshot (nothing to compare against) is
        reported as stable -- there is no evidence of instability in
        what was observed, which is what this field represents; it is
        not a claim that the route is guaranteed unchanging beyond the
        observed window."""
        return self.change_count == 0


def analyze_route_churn(snapshots: list[RouteSnapshot]) -> RouteChurnResult:
    """Detect route changes across an ordered sequence of
    RouteSnapshots for the same target.

    `snapshots` must already be ordered by time (oldest first) -- this
    function does not sort them; it compares consecutive pairs exactly
    as given. Two consecutive snapshots are considered "changed" when
    their signature() differs (see this module's own docstring for why
    that fingerprint is immune to lookup-metadata-only changes).

    Raises ValueError if given an empty sequence, or if the snapshots
    don't all share the same target -- mixing targets would produce a
    meaningless churn count, so this is rejected explicitly rather than
    silently producing a nonsensical result.
    """
    if not snapshots:
        raise ValueError("analyze_route_churn requires at least one RouteSnapshot")

    target = snapshots[0].target
    for snap in snapshots:
        if snap.target != target:
            raise ValueError(
                f"all snapshots must share the same target; found {snap.target!r} "
                f"alongside {target!r}"
            )

    unique_signatures = {snap.signature() for snap in snapshots}

    changes: list[RouteChange] = []
    for previous, current in zip(snapshots, snapshots[1:]):
        previous_signature = previous.signature()
        current_signature = current.signature()
        if previous_signature != current_signature:
            changes.append(
                RouteChange(
                    previous_signature=previous_signature,
                    new_signature=current_signature,
                    previous_timestamp=previous.timestamp,
                    new_timestamp=current.timestamp,
                )
            )

    return RouteChurnResult(
        target=target,
        snapshot_count=len(snapshots),
        unique_signature_count=len(unique_signatures),
        changes=changes,
    )
