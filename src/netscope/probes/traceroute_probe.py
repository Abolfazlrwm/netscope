"""
netscope.probes.traceroute_probe

Traceroute measurement using icmplib.traceroute() (LGPL-3.0-or-later,
already an existing, license-cleared dependency -- see
adr-003-traceroute-strategy.md). Mirrors icmp_probe.py's pattern: this
module contains the raw measurement logic (calling the library,
mapping its result/exceptions onto RawMeasurement); the adapter layer
(adapters/probes/traceroute_adapter.py) only translates the Probe
Protocol's call shape into a call to this module's function, exactly
like every other probe in this project.

Converts icmplib's `list[Hop]` into a netscope.core.models.RouteSnapshot
(a list of RouteHop), carried inside the returned RawMeasurement.extra,
exactly as core.ports.Probe's own docstring already anticipates:
"Adapters that produce route data (traceroute) return RawMeasurement
too, with hop details carried in RawMeasurement.extra... this contract
does not introduce a second result type for that case."

PRIVILEGE HANDLING (ADR-003's explicit requirement): unlike
icmp_probe.ping(), icmplib.traceroute() has no unprivileged fallback
mode -- it requires root/Administrator unconditionally (confirmed
directly against the installed icmplib package). On
icmplib.exceptions.SocketPermissionError, this returns a
RawMeasurement-shaped failure with
error_type=ProbeErrorType.PERMISSION_DENIED (an already-existing
ProbeErrorType value, no new one needed) rather than raising or
silently retrying -- there is nothing to retry into, per ADR-003.

hostname/asn/organization/country on each RouteHop are intentionally
left at their existing defaults (None) here -- per
architecture-overview.md SS5 and ADR-003, reverse-DNS/ASN/GeoIP lookups
are separate, later adapter responsibilities (future-roadmap.md
TASK-022/023), not this probe's job. Finalizing those Hop fields is
explicitly TASK-020's scope, not this one's.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import (
    ProbeErrorType,
    ProbeType,
    RawMeasurement,
    RouteHop,
    RouteSnapshot,
    utcnow,
)

try:
    import icmplib
    _ICMPLIB_AVAILABLE = True
except ImportError:
    _ICMPLIB_AVAILABLE = False


def traceroute(host: str, **options: Any) -> RawMeasurement:
    if not _ICMPLIB_AVAILABLE:
        return RawMeasurement(
            probe_type=ProbeType.TRACEROUTE,
            target=host,
            success=False,
            error="icmplib not installed",
            error_type=ProbeErrorType.PROBE_UNAVAILABLE,
        )

    try:
        icmplib_hops = icmplib.traceroute(host, **options)
    except icmplib.exceptions.SocketPermissionError as exc:
        # ADR-003's required behavior: no retry, no unprivileged
        # fallback exists for traceroute (unlike ping) -- fail loudly
        # and immediately with a structured error type so the eventual
        # UI can tell the user why and what to do (run elevated, or use
        # the documented system-binary fallback adapter -- not this
        # probe's concern).
        return RawMeasurement(
            probe_type=ProbeType.TRACEROUTE,
            target=host,
            timestamp=utcnow(),
            success=False,
            error=str(exc),
            error_type=ProbeErrorType.PERMISSION_DENIED,
        )
    except Exception as exc:  # noqa: BLE001
        return RawMeasurement(
            probe_type=ProbeType.TRACEROUTE,
            target=host,
            timestamp=utcnow(),
            success=False,
            error=str(exc),
            error_type=ProbeErrorType.UNKNOWN,
        )

    route_hops = [
        RouteHop(
            ttl=hop.distance,
            address=hop.address,
            hostname=None,
            avg_rtt_ms=hop.avg_rtt if hop.is_alive else None,
            packet_loss_pct=hop.packet_loss * 100,
        )
        for hop in icmplib_hops
    ]
    snapshot = RouteSnapshot(target=host, timestamp=utcnow(), hops=route_hops)

    # "Success" here means the probe produced route data, not that the
    # destination was definitely reached -- deciding whether an
    # incomplete route (e.g. a firewall silently dropping the final
    # hop) counts as a "problem" is diagnosis-layer judgment
    # (core.diagnosis, not yet built), which a probe must not make per
    # the architecture's own core principle: "A probe should NOT
    # decide... The diagnosis layer interprets evidence." A route with
    # fewer hops than expected is still useful evidence, not an error,
    # as long as some hops were found at all.
    return RawMeasurement(
        probe_type=ProbeType.TRACEROUTE,
        target=host,
        timestamp=utcnow(),
        success=len(route_hops) > 0,
        error=None if route_hops else "traceroute produced no hops",
        extra={"route": snapshot},
    )
