"""
netscope.core.drift

The drift watch (v0.15) -- the slow leak that the per-check score
cannot see.

WHY THIS MODULE EXISTS
----------------------
Every judgment NetScope made until now compares a SINGLE sweep against
the learned baseline (core/scoring.py) or against this user's own
history of the same target (core/baseline.py's sigma verdicts,
core/routing.py's churn). Both lenses have one structural blind spot:
the baseline itself is a trailing average of the very measurements it
judges. If the line quietly rots -- an ISP's peering degrading week
over week, an aging router, a resolver that decays by a millisecond a
day -- every individual sweep still reads "excellent", because the
baseline rots with it. Nothing was watching the TREND.

Drift is the complementary lens, and it needs no baseline at all: the
stored measurement log is split into two adjacent windows (the recent
one, and the same-length window before it), each vital is aggregated
per window with a robust statistic, and the windows are compared
against each other. A median latency of 23ms last week and 31ms this
week is degradation NO single check ever flagged -- and no learned
threshold would, because both windows fed the same baseline.

WHY THIS LIVES IN core
----------------------
Pure domain computation over `core.models.RawMeasurement` lists: no
I/O, no adapters, no persistence, no UI -- the same dependency rule
core/scoring.py documents for itself (adr-001-architecture-style.md).
The caller (app/use_cases.py's `run_drift_analysis`) reads the log and
injects `now`; this module never touches a repository and never calls
utcnow() itself, so the analysis is fully reproducible from a fixed
clock -- the same injectable-`until` discipline build_evidence_report
established.

WHAT IS COMPARED (THE FOUR VITALS)
----------------------------------
- latency  -- median of successful icmp readings. Median, not mean:
  a window is a population of sweeps, and a few congested sweeps must
  not move the window's story (the same robustness the speed command's
  own "median Mbps" convention chose).
- packet loss -- MEAN of successful icmp readings. Deliberately the
  one asymmetric choice: loss readings are almost always 0.0 with
  spikes, so a median would read 0.0 for every window ever -- the mean
  is the honest carrier of "how often did it actually drop".
- jitter   -- median of successful icmp readings (v0.13's third vital
  sign joins the watch; its store on UserBaseline was precedent for
  giving each metric its own lens, and drift gives it its own window).
- throughput -- median of successful bandwidth readings'
  extra["throughput_mbps"] (v0.4's vital; the same value the baseline's
  throughput store observes).

A vital with no probe in a window is reported as insufficient rather
than silently omitted -- an absent reading is a fact about coverage,
and the watch discloses coverage instead of pretending.

THRESHOLDS -- NONE OF THEM ARE NEW ARBITRARY NUMBERS
----------------------------------------------------
The maturity rule is borrowed whole: a window with fewer than
_MATURE_SAMPLE_COUNT samples has no opinion, exactly as
MetricBaseline.deviation_sigma() refuses to produce a signal below the
same 5 (core/baseline.py, reused by scoring and diagnosis before this).
The throughput band is borrowed whole: THROUGHPUT_VERDICT_BAND_PCT is
already the percentage at which a speed test is called slower/faster
than usual -- a drift beyond that same band means the same sentence at
window scale. The latency/jitter/loss thresholds are new constants and
say so explicitly (see their definitions below): latency and jitter
need a relative AND an absolute condition because a purely relative
rule cries wolf on small medians, while loss needs a purely absolute
one because relative change from a 0.2% floor is meaningless.

DIRECTION
---------
Latency, loss and jitter are worse-when-higher. Throughput is the
mirror: a download that shrinks is degradation, and THROUGHPUT
_VERDICT_BAND_PCT already governs both directions of that sentence
(compare_throughput's band is symmetric), so the watch mirrors it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from netscope.core.baseline import THROUGHPUT_VERDICT_BAND_PCT
from netscope.core.models import ProbeType, RawMeasurement

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A window with fewer samples than this has no opinion -- the exact
# maturity MetricBaseline.deviation_sigma() already demands (5) before
# it produces a real signal, and that scoring.py and diagnosis.py both
# reused for their analogous "the baseline is too young to judge"
# problems. The watch is the same rule applied to a window instead of
# a running mean.
_MATURE_SAMPLE_COUNT = 5

# NEW CONSTANTS, SAYING SO: the latency drift rule needs BOTH a
# relative and an absolute condition. A purely relative rule cries wolf
# on small medians (20ms -> 23ms is +15% and unfelt), a purely absolute
# one cries wolf on slow links (200ms -> 210ms is noise on a satellite
# hop). Both must hold: 5ms is roughly the smallest change a human
# feels on a responsive line, and 25% is wide enough that ordinary
# window-to-window jitter does not fire it.
LATENCY_DRIFT_MIN_PCT = 25.0
LATENCY_DRIFT_MIN_ABS_MS = 5.0

# Same both-conditions structure for jitter, wider on both axes:
# delay variation is naturally small and spiky, so the relative band
# must be wider than latency's and the absolute floor smaller.
JITTER_DRIFT_MIN_PCT = 30.0
JITTER_DRIFT_MIN_ABS_MS = 1.5

# Loss is the one purely-absolute rule: from a 0.2% floor, +100% is
# +0.2 points -- relative change is meaningless there. Two full
# percentage points of window-mean loss is the smallest signal that is
# not sampling noise.
LOSS_DRIFT_MIN_ABS_PCT = 2.0

# The throughput band is NOT new: THROUGHPUT_VERDICT_BAND_PCT (15.0,
# imported) is already the percentage at which compare_throughput
# calls a speed test slower/faster than usual. A window-to-window
# move beyond that same band is the same sentence at window scale.

# The machine-readable verdicts, shared by the JSON payload and the
# rendered table -- the same vocabulary discipline core/baseline.py's
# VERDICT_* constants set.
DRIFT_STABLE = "stable"
DRIFT_DEGRADING = "degrading"
DRIFT_IMPROVING = "improving"
DRIFT_INSUFFICIENT = "insufficient_history"

# Overall verdicts are the same words, plus the report-level state of
# having nothing to judge at all.
OVERALL_INSUFFICIENT = DRIFT_INSUFFICIENT

VITAL_LATENCY = "latency"
VITAL_LOSS = "packet_loss"
VITAL_JITTER = "jitter"
VITAL_THROUGHPUT = "throughput"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VitalDrift:
    """One vital's window-to-window comparison.

    `older_stat`/`recent_stat` are the window aggregates (median for
    latency/jitter/throughput, mean for loss). `change_abs` and
    `change_pct` are signed: positive means the recent window is HIGHER
    than the older one (for throughput, higher is better even though
    the sign convention is the same -- direction is the verdict's job,
    never the reader's arithmetic). `change_pct` is None when the older
    window's statistic is exactly 0 and the ratio would divide by zero.

    `detail` is the one human sentence this vital contributes to the
    evidence list -- deterministic, number-citing, in the same style
    the diagnosis engine's evidence strings set.
    """

    vital: str
    verdict: str
    older_stat: float | None
    recent_stat: float | None
    change_abs: float | None
    change_pct: float | None
    older_samples: int
    recent_samples: int
    detail: str


@dataclass(frozen=True)
class DriftReport:
    """The full drift-watch result for one target.

    Windows are documented, not implied: `window_start` opens the older
    window, `boundary` closes it and opens the recent one, `end` closes
    the recent one -- both ends inclusive, matching the measurement
    repository's own boundary semantics. `vitals` always carries all
    four vitals the watch knows (insufficient ones included, with their
    sample counts -- coverage is evidence too); `evidence` carries only
    the sentences worth reading.
    """

    target: str
    recent_days: int
    window_start: datetime
    boundary: datetime
    end: datetime
    overall: str
    vitals: tuple[VitalDrift, ...]
    evidence: tuple[str, ...]


# ---------------------------------------------------------------------------
# Window statistics
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _relative_change(older: float, recent: float) -> float | None:
    """Signed percentage change older -> recent, or None when the older
    value is exactly 0 and the ratio would divide by zero."""
    if older == 0.0:
        return None
    return (recent - older) / older * 100.0


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "--"
    return f"{value:.1f}{unit}"


# ---------------------------------------------------------------------------
# The four vitals
# ---------------------------------------------------------------------------


def _icmp_values(
    measurements: list[RawMeasurement],
    attr: str,
) -> list[float]:
    """Successful icmp readings of one metric. Failed measurements
    never feed a vital even when stale numeric fields ride along on
    them -- a timeout's residual numbers are not evidence of latency."""
    values: list[float] = []
    for m in measurements:
        if m.probe_type is not ProbeType.ICMP or not m.success:
            continue
        value = getattr(m, attr)
        if value is not None:
            values.append(float(value))
    return values


def _throughput_values(measurements: list[RawMeasurement]) -> list[float]:
    """Successful bandwidth readings' throughput_mbps. The value must
    actually be a number -- a bandwidth row with an empty or non-numeric
    extra block contributes nothing rather than crashing the watch."""
    values: list[float] = []
    for m in measurements:
        if m.probe_type is not ProbeType.BANDWIDTH or not m.success:
            continue
        raw = m.extra.get("throughput_mbps")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        values.append(float(raw))
    return values


def _window_slice(
    measurements: list[RawMeasurement],
    *,
    start: datetime,
    end: datetime,
) -> list[RawMeasurement]:
    """Inclusive [start, end] slice -- the measurement repository's own
    boundary semantics, applied to an in-memory pool so the watch's
    windows and the log's windows cannot disagree."""
    return [m for m in measurements if start <= m.timestamp <= end]


# ---------------------------------------------------------------------------
# Per-vital verdicts
# ---------------------------------------------------------------------------


def _both_conditions_verdict(
    *,
    older: float | None,
    recent: float | None,
    older_samples: int,
    recent_samples: int,
    vital: str,
    unit: str,
    min_pct: float,
    min_abs: float,
    worse_when_higher: bool,
) -> VitalDrift:
    """The shared latency/jitter rule: BOTH a relative and an absolute
    condition must hold in the same direction for a non-stable verdict.
    The mirror is symmetric -- a symmetric threshold band means
    "improving" is the same sentence with the sign flipped, not a
    second, looser rule."""
    label = f"median {vital}"
    if older is None or recent is None or older_samples < _MATURE_SAMPLE_COUNT or recent_samples < _MATURE_SAMPLE_COUNT:
        return VitalDrift(
            vital=vital,
            verdict=DRIFT_INSUFFICIENT,
            older_stat=older,
            recent_stat=recent,
            change_abs=None,
            change_pct=None,
            older_samples=older_samples,
            recent_samples=recent_samples,
            detail=f"{label}: not enough stored rounds to judge drift yet "
            f"({older_samples} vs {recent_samples} usable readings, {_MATURE_SAMPLE_COUNT} per window needed)",
        )
    change_abs = recent - older
    change_pct = _relative_change(older, recent)
    degrading = (
        (change_abs >= min_abs and change_pct is not None and change_pct >= min_pct)
        if worse_when_higher
        else (change_abs <= -min_abs and change_pct is not None and change_pct <= -min_pct)
    )
    improving = (
        (change_abs <= -min_abs and change_pct is not None and change_pct <= -min_pct)
        if worse_when_higher
        else (change_abs >= min_abs and change_pct is not None and change_pct >= min_pct)
    )
    if degrading:
        verdict = DRIFT_DEGRADING
        phrase = "the recent window is consistently worse than the one before it"
    elif improving:
        verdict = DRIFT_IMPROVING
        phrase = "the recent window is consistently better than the one before it"
    else:
        verdict = DRIFT_STABLE
        phrase = "the two windows read the same"
    detail = f"{label} {_fmt(older, unit)} -> {_fmt(recent, unit)} ({_fmt_pct(change_pct)}) across the two windows -- {phrase}"
    return VitalDrift(
        vital=vital,
        verdict=verdict,
        older_stat=older,
        recent_stat=recent,
        change_abs=change_abs,
        change_pct=change_pct,
        older_samples=older_samples,
        recent_samples=recent_samples,
        detail=detail,
    )


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.0f}%"


def _loss_verdict(
    *,
    older: float | None,
    recent: float | None,
    older_samples: int,
    recent_samples: int,
) -> VitalDrift:
    """The loss rule: purely absolute (see LOSS_DRIFT_MIN_ABS_PCT for
    why no relative condition exists)."""
    if older is None or recent is None or older_samples < _MATURE_SAMPLE_COUNT or recent_samples < _MATURE_SAMPLE_COUNT:
        return VitalDrift(
            vital=VITAL_LOSS,
            verdict=DRIFT_INSUFFICIENT,
            older_stat=older,
            recent_stat=recent,
            change_abs=None,
            change_pct=None,
            older_samples=older_samples,
            recent_samples=recent_samples,
            detail="packet loss: not enough stored rounds to judge drift yet "
            f"({older_samples} vs {recent_samples} usable readings, {_MATURE_SAMPLE_COUNT} per window needed)",
        )
    change_abs = recent - older
    if change_abs >= LOSS_DRIFT_MIN_ABS_PCT:
        verdict, phrase = DRIFT_DEGRADING, "packets are dropping measurably more often than the window before"
    elif change_abs <= -LOSS_DRIFT_MIN_ABS_PCT:
        verdict, phrase = DRIFT_IMPROVING, "packets are dropping measurably less often than the window before"
    else:
        verdict, phrase = DRIFT_STABLE, "the two windows lose packets at the same rate"
    detail = f"mean packet loss {_fmt(older, '%')} -> {_fmt(recent, '%')} ({change_abs:+.1f}pp) across the two windows -- {phrase}"
    return VitalDrift(
        vital=VITAL_LOSS,
        verdict=verdict,
        older_stat=older,
        recent_stat=recent,
        change_abs=change_abs,
        change_pct=_relative_change(older, recent),
        older_samples=older_samples,
        recent_samples=recent_samples,
        detail=detail,
    )


def _throughput_verdict(
    *,
    older: float | None,
    recent: float | None,
    older_samples: int,
    recent_samples: int,
) -> VitalDrift:
    """The throughput rule: relative only, and the band is borrowed
    whole from compare_throughput -- a download is a percentage story
    ("your line is 20% slower"), never an absolute-Mbps story across
    different plans and endpoints."""
    if older is None or recent is None or older_samples < _MATURE_SAMPLE_COUNT or recent_samples < _MATURE_SAMPLE_COUNT:
        return VitalDrift(
            vital=VITAL_THROUGHPUT,
            verdict=DRIFT_INSUFFICIENT,
            older_stat=older,
            recent_stat=recent,
            change_abs=None,
            change_pct=None,
            older_samples=older_samples,
            recent_samples=recent_samples,
            detail="throughput: not enough stored speed tests to judge drift yet "
            f"({older_samples} vs {recent_samples} usable readings, {_MATURE_SAMPLE_COUNT} per window needed)",
        )
    change_abs = recent - older
    change_pct = _relative_change(older, recent)
    # Direction is the mirror of the other vitals: a SHRINKING download
    # is degradation. The band is symmetric, exactly as
    # compare_throughput's own verdict band is.
    degrading = change_pct is not None and change_pct <= -THROUGHPUT_VERDICT_BAND_PCT
    improving = change_pct is not None and change_pct >= THROUGHPUT_VERDICT_BAND_PCT
    if degrading:
        verdict, phrase = DRIFT_DEGRADING, "the recent window downloads consistently slower than the one before"
    elif improving:
        verdict, phrase = DRIFT_IMPROVING, "the recent window downloads consistently faster than the one before"
    else:
        verdict, phrase = DRIFT_STABLE, "the two windows download at the same pace"
    detail = f"median throughput {_fmt(older, 'Mbps')} -> {_fmt(recent, 'Mbps')} ({_fmt_pct(change_pct)}) across the two windows -- {phrase}"
    return VitalDrift(
        vital=VITAL_THROUGHPUT,
        verdict=verdict,
        older_stat=older,
        recent_stat=recent,
        change_abs=change_abs,
        change_pct=change_pct,
        older_samples=older_samples,
        recent_samples=recent_samples,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# The watch
# ---------------------------------------------------------------------------


def _overall_verdict(vitals: list[VitalDrift]) -> str:
    """The report-level verdict: degradation outranks improvement (a
    watch that averages a leaking line against a improving one into
    "stable" has missed its own point), and "insufficient" only when
    NO vital managed to judge anything."""
    verdicts = [v.verdict for v in vitals]
    if DRIFT_DEGRADING in verdicts:
        return DRIFT_DEGRADING
    if DRIFT_IMPROVING in verdicts:
        return DRIFT_IMPROVING
    if all(v == DRIFT_INSUFFICIENT for v in verdicts):
        return OVERALL_INSUFFICIENT
    return DRIFT_STABLE


def analyze_drift(
    measurements: list[RawMeasurement],
    *,
    target: str,
    now: datetime,
    recent_days: int = 7,
) -> DriftReport:
    """Compare the recent window of stored history for `target` against
    the same-length window before it, vital by vital.

    `now` is injected, never assumed: the caller pins the clock (the
    same reproducibility rule build_evidence_report's `until` set), so
    an analysis can be re-run and compared byte for byte. The older
    window is [now - 2*recent_days, now - recent_days], the recent
    window is [now - recent_days, now]; both ends inclusive; anything
    outside the two windows is ignored, however extreme -- a watch that
    lets a month-old reading dominate the comparison is not a watch.

    `recent_days` must be a positive integer, matching
    build_evidence_report's own days validation -- a zero or negative
    window is a caller bug, not a valid query for nothing.
    """
    if recent_days <= 0:
        raise ValueError(f"recent_days must be a positive integer, got {recent_days!r}")

    end = now
    boundary = now - timedelta(days=recent_days)
    window_start = now - timedelta(days=2 * recent_days)

    older_pool = _window_slice(measurements, start=window_start, end=boundary)
    recent_pool = _window_slice(measurements, start=boundary, end=end)

    # Each vital's readings are extracted exactly once per window -- the
    # median/mean AND the sample count come from the same list, so the
    # statistic and its coverage can never disagree.
    older_latency_values = _icmp_values(older_pool, "latency_ms")
    recent_latency_values = _icmp_values(recent_pool, "latency_ms")
    older_loss_values = _icmp_values(older_pool, "packet_loss_pct")
    recent_loss_values = _icmp_values(recent_pool, "packet_loss_pct")
    older_jitter_values = _icmp_values(older_pool, "jitter_ms")
    recent_jitter_values = _icmp_values(recent_pool, "jitter_ms")
    older_throughput_values = _throughput_values(older_pool)
    recent_throughput_values = _throughput_values(recent_pool)

    latency = _both_conditions_verdict(
        older=_median(older_latency_values),
        recent=_median(recent_latency_values),
        older_samples=len(older_latency_values),
        recent_samples=len(recent_latency_values),
        vital=VITAL_LATENCY,
        unit="ms",
        min_pct=LATENCY_DRIFT_MIN_PCT,
        min_abs=LATENCY_DRIFT_MIN_ABS_MS,
        worse_when_higher=True,
    )
    jitter = _both_conditions_verdict(
        older=_median(older_jitter_values),
        recent=_median(recent_jitter_values),
        older_samples=len(older_jitter_values),
        recent_samples=len(recent_jitter_values),
        vital=VITAL_JITTER,
        unit="ms",
        min_pct=JITTER_DRIFT_MIN_PCT,
        min_abs=JITTER_DRIFT_MIN_ABS_MS,
        worse_when_higher=True,
    )
    loss = _loss_verdict(
        older=_mean(older_loss_values),
        recent=_mean(recent_loss_values),
        older_samples=len(older_loss_values),
        recent_samples=len(recent_loss_values),
    )
    throughput = _throughput_verdict(
        older=_median(older_throughput_values),
        recent=_median(recent_throughput_values),
        older_samples=len(older_throughput_values),
        recent_samples=len(recent_throughput_values),
    )

    vitals = [latency, loss, jitter, throughput]
    evidence = tuple(v.detail for v in vitals if v.verdict != DRIFT_INSUFFICIENT)

    return DriftReport(
        target=target,
        recent_days=recent_days,
        window_start=window_start,
        boundary=boundary,
        end=end,
        overall=_overall_verdict(vitals),
        vitals=tuple(vitals),
        evidence=evidence,
    )
