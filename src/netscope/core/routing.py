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
class RouteHopChange:
    """One hop position whose address differed between two consecutive
    RouteSnapshots -- the hop-level "what actually changed" detail behind
    a RouteChange (v0.8; before then a change was only "the signatures
    differed", with no record of WHICH hops moved).

    Positional, not address-keyed: hops are paired by their index in
    each snapshot's hop list -- the same pairing `RouteSnapshot.signature()`
    compares, so what this diff reports always agrees with what the
    signature says changed. A hop that answered last run and timed out
    this run appears as previous_address set / new_address None; a hop
    that appeared beyond the old path's end is the reverse. `ttl` is the
    position's label in whichever snapshot still has a hop there (the
    new snapshot's when it does, the previous one's otherwise)."""

    ttl: int
    previous_address: str | None
    new_address: str | None


@dataclass
class RouteChange:
    """One detected transition between two consecutive RouteSnapshots
    for the same target -- their signature() differed."""

    previous_signature: str
    new_signature: str
    previous_timestamp: datetime
    new_timestamp: datetime
    changed_hops: list[RouteHopChange] = field(default_factory=list)


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
    # The observed window's boundaries -- the timestamps of the oldest
    # and newest snapshots the analysis actually saw (None only when a
    # RouteChurnResult was built by hand, outside analyze_route_churn;
    # the function itself always has at least one snapshot to report).
    window_start: datetime | None = None
    window_end: datetime | None = None

    @property
    def change_count(self) -> int:
        return len(self.changes)

    @property
    def last_change(self) -> RouteChange | None:
        """The most recent transition in the window, or None when the
        path never changed -- the change a UI leads with when it has
        room for exactly one detail line."""
        return self.changes[-1] if self.changes else None

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
            raise ValueError(f"all snapshots must share the same target; found {snap.target!r} alongside {target!r}")

    unique_signatures = {snap.signature() for snap in snapshots}

    changes: list[RouteChange] = []
    for previous, current in zip(snapshots, snapshots[1:], strict=False):
        previous_signature = previous.signature()
        current_signature = current.signature()
        if previous_signature != current_signature:
            changes.append(
                RouteChange(
                    previous_signature=previous_signature,
                    new_signature=current_signature,
                    previous_timestamp=previous.timestamp,
                    new_timestamp=current.timestamp,
                    changed_hops=_diff_route_hops(previous, current),
                )
            )

    return RouteChurnResult(
        target=target,
        snapshot_count=len(snapshots),
        unique_signature_count=len(unique_signatures),
        changes=changes,
        window_start=snapshots[0].timestamp,
        window_end=snapshots[-1].timestamp,
    )


def _diff_route_hops(previous: RouteSnapshot, current: RouteSnapshot) -> list[RouteHopChange]:
    """Which hop positions differ between two consecutive snapshots,
    paired by position -- the same pairing signature() performs (see
    RouteHopChange's docstring for why positions, not addresses).

    Length mismatch is part of the diff, not an error: a path that grew
    or shrank between runs changes at exactly the positions the two hop
    lists disagree, plus every position one list has and the other
    doesn't. Two snapshots with identical signatures can never reach
    this function (the caller only diffs on a signature change)."""
    hop_changes: list[RouteHopChange] = []
    for position in range(max(len(previous.hops), len(current.hops))):
        previous_hop = previous.hops[position] if position < len(previous.hops) else None
        current_hop = current.hops[position] if position < len(current.hops) else None
        previous_address = previous_hop.address if previous_hop is not None else None
        current_address = current_hop.address if current_hop is not None else None
        if previous_address == current_address:
            continue
        label_hop = current_hop if current_hop is not None else previous_hop
        if label_hop is None:
            # Both sides missing: their addresses compared equal (None),
            # so this position was already skipped above -- kept explicit
            # so `ttl` never reads off a None hop.
            continue
        hop_changes.append(
            RouteHopChange(
                ttl=label_hop.ttl,
                previous_address=previous_address,
                new_address=current_address,
            )
        )
    return hop_changes
