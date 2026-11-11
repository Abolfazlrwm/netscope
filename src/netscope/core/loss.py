"""
netscope.core.loss

Packet-loss confirmation policy (v0.9): a single ICMP sweep that comes
back with partial loss is a *suspicion*, not a finding. icmplib's default
sweep is four packets -- one dropped packet reads as 25% loss, and one
stray drop on a busy Wi-Fi hop is routine. Before v0.9 every partial-loss
reading went straight into scoring, the baseline and the diagnosis as if
it were a measured property of the link, which it was not: it was one
four-packet sample.

The policy here is the classic confirm-then-report pattern:

1. a sweep returns successful (some packets answered) with 0 < loss < 100
   -- partial loss, worth a second opinion;
2. the caller runs a few extra sweeps against the same target (the *app*
   layer owns the running; this module stays pure);
3. `merge_loss_rounds` folds all sweeps into one `RawMeasurement` whose
   loss number is the window average and whose `extra` carries the honest
   story: per-round losses, and whether the loss *reproduced* ("confirmed")
   or did not ("transient").

WHY THIS LIVES IN CORE
----------------------
Both functions are pure domain policy over core models -- no sockets, no
I/O, no registry. Deciding *when a reading warrants more evidence* and
*how several readings fold into one* is exactly the kind of judgment
core owns (the same reason churn analysis lives in `core/routing.py`);
actually re-running the probe is execution, which stays in `app` (the
same reason the baseline observe/persist wiring lives there).

WHAT THIS MODULE DOES NOT DO
----------------------------
- It does not touch total failure (100% loss / success=False): a dead
  target is not a sampling question, and the probe adapters already
  report that case with structured errors.
- It does not re-interpret a merged measurement: `needs_loss_confirmation`
  returns False for anything carrying a `loss_rounds` marker, so an
  already-merged window can never be re-confirmed into a window of
  windows.
- It knows nothing about which probe type produced the measurement --
  the trigger is purely the (success, partial loss) shape, so the app
  layer may confirm any probe whose readings have that shape.
"""

from __future__ import annotations

from netscope.core.models import RawMeasurement

# How many *extra* sweeps run once partial loss is seen. Two is the
# smallest number that distinguishes "one stray drop" (0% twice more)
# from "the loss is still there" (loss again) without tripling the
# wall-clock of every lossy check.
LOSS_CONFIRMATION_ROUNDS = 2

# `extra` key carrying the per-round loss numbers on a merged
# measurement -- also the marker that a measurement has already been
# through this policy (see needs_loss_confirmation).
_LOSS_ROUNDS_KEY = "loss_rounds"


def needs_loss_confirmation(measurement: RawMeasurement) -> bool:
    """Whether `measurement` shows the partial-loss shape worth a second
    opinion: the sweep *succeeded* (something answered -- total failure
    is a finding, not a suspicion) and reported a loss strictly between
    0% and 100%.

    A measurement that already carries a `loss_rounds` marker is a merged
    window, never re-confirmed -- this is what keeps the policy idempotent
    if a caller ever feeds a merged measurement back through it.
    """
    if _LOSS_ROUNDS_KEY in measurement.extra:
        return False
    if not measurement.success:
        return False
    loss = measurement.packet_loss_pct
    return loss is not None and 0 < loss < 100


def merge_loss_rounds(rounds: list[RawMeasurement]) -> RawMeasurement:
    """Fold the original sweep and its confirmation sweeps into one
    `RawMeasurement` describing the whole observation window.

    Aggregation rules, each documented because each is a choice:

    - `packet_loss_pct`: the mean of every round that reported a loss
      number -- the window's average loss, not its worst round. The
      per-round numbers ride along in `extra["loss_rounds"]`, so nothing
      is hidden by the averaging.
    - `latency_ms` / `jitter_ms`: the mean of the rounds that reported
      each -- a round that lost packets still reports the RTT of the
      ones that arrived, and that is real signal.
    - `success`: True if *any* round got an answer (the target is
      reachable; the window says "reachable, lossy", not "dead").
    - `error`/`error_type`: kept from the first round only when the whole
      window failed -- a window with one answering round has no error.
    - `timestamp`: the *last* round's -- the merged reading is current
      as of the final sweep, which is when the window closed.
    - `extra`: the first round's probe-specific extras (min/max RTT,
      resolved addresses, whatever the adapter recorded) survive, with
      the window-level keys recomputed where every round reports them:
      min_rtt_ms takes the minimum of minimums, max_rtt_ms the maximum
      of maximums, packets_sent/packets_received the sums. On top come
      the confirmation keys:
        - `loss_rounds`: per-round loss numbers (None where a round
          reported no loss figure), in run order, first sweep first;
        - `loss_confirmed`: True if *any* sweep after the first also
          saw loss -- the loss reproduced; False if every confirmation
          sweep was clean -- a transient, worth knowing it healed.

    `rounds` must be non-empty; the first entry is treated as the
    original sweep and the rest as confirmations. The function is pure:
    it never mutates the rounds it is given.
    """
    if not rounds:
        raise ValueError("merge_loss_rounds needs at least one round")

    first, last = rounds[0], rounds[-1]
    losses = [r.packet_loss_pct for r in rounds if r.packet_loss_pct is not None]
    latencies = [r.latency_ms for r in rounds if r.success and r.latency_ms is not None]
    jitters = [r.jitter_ms for r in rounds if r.jitter_ms is not None]

    success = any(r.success for r in rounds)
    confirmation_rounds = rounds[1:]
    confirmed = any((r.packet_loss_pct is not None and r.packet_loss_pct > 0) for r in confirmation_rounds)

    extra: dict = dict(first.extra)
    extra[_LOSS_ROUNDS_KEY] = [r.packet_loss_pct for r in rounds]
    extra["loss_confirmed"] = confirmed
    extra["loss_window_rounds"] = len(rounds)
    if "min_rtt_ms" in extra:
        mins = [r.extra["min_rtt_ms"] for r in rounds if "min_rtt_ms" in r.extra]
        extra["min_rtt_ms"] = min(mins)
    if "max_rtt_ms" in extra:
        maxs = [r.extra["max_rtt_ms"] for r in rounds if "max_rtt_ms" in r.extra]
        extra["max_rtt_ms"] = max(maxs)
    for key in ("packets_sent", "packets_received"):
        if key in extra:
            extra[key] = sum(r.extra[key] for r in rounds if key in r.extra)

    return RawMeasurement(
        probe_type=first.probe_type,
        target=first.target,
        timestamp=last.timestamp,
        success=success,
        latency_ms=sum(latencies) / len(latencies) if latencies else None,
        packet_loss_pct=sum(losses) / len(losses) if losses else None,
        jitter_ms=sum(jitters) / len(jitters) if jitters else None,
        error=first.error if not success else None,
        error_type=first.error_type if not success else None,
        extra=extra,
    )
