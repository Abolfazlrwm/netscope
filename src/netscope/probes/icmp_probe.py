"""
ICMP probe.

Uses `icmplib` (LGPL-3.0) as a library dependency. We do not vendor or
modify icmplib's source -- it is installed normally via pip and imported.
"""

from __future__ import annotations

from netscope.core.models import ProbeType, RawMeasurement, utcnow

try:
    import icmplib
    _ICMPLIB_AVAILABLE = True
except ImportError:
    _ICMPLIB_AVAILABLE = False


def ping(target: str, count: int = 4, timeout: float = 2.0, privileged: bool = True) -> RawMeasurement:
    """Run an ICMP ping sweep against `target` and return a RawMeasurement.

    `privileged=False` uses an unprivileged ICMP socket where supported
    (no root/administrator required on most modern OSes), falling back
    to privileged mode on failure.
    """
    if not _ICMPLIB_AVAILABLE:
        return RawMeasurement(
            probe_type=ProbeType.ICMP,
            target=target,
            success=False,
            error="icmplib not installed",
        )

    try:
        host = icmplib.ping(target, count=count, timeout=timeout, privileged=privileged)
    except icmplib.exceptions.SocketPermissionError:
        try:
            host = icmplib.ping(target, count=count, timeout=timeout, privileged=False)
        except Exception as exc:  # noqa: BLE001
            return RawMeasurement(
                probe_type=ProbeType.ICMP,
                target=target,
                success=False,
                error=f"permission error, unprivileged fallback also failed: {exc}",
            )
    except Exception as exc:  # noqa: BLE001
        return RawMeasurement(
            probe_type=ProbeType.ICMP,
            target=target,
            success=False,
            error=str(exc),
        )

    return RawMeasurement(
        probe_type=ProbeType.ICMP,
        target=target,
        timestamp=utcnow(),
        success=host.is_alive,
        latency_ms=host.avg_rtt if host.is_alive else None,
        packet_loss_pct=host.packet_loss * 100,
        jitter_ms=getattr(host, "jitter", None),
        extra={
            "min_rtt_ms": host.min_rtt,
            "max_rtt_ms": host.max_rtt,
            "packets_sent": host.packets_sent,
            "packets_received": host.packets_received,
        },
    )
