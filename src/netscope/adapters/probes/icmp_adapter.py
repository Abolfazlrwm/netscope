"""
Adapter making the existing netscope.probes.icmp_probe.ping() function
satisfy netscope.core.ports.Probe.

Contains no measurement logic of its own -- icmp_probe.py is not modified
and is not reimplemented here. This class only translates the Probe
Protocol's run(target, **options) call shape into a call to the existing
ping(target, count, timeout, privileged) function, and returns whatever
it returns, unchanged except for one addition (TASK-014, see below).

TASK-014 addition: classifies icmp_probe.ping()'s existing free-text
`error` string into a structured ProbeErrorType, attached to the
returned RawMeasurement's `error_type` field. icmp_probe.py's own
measurement logic -- its try/except tree, its icmplib calls -- is
completely unmodified; classification happens entirely here, in the
adapter, working only from what icmp_probe.py already returns. This is
the smallest change that gives ICMP structured errors without
redesigning icmp_probe.py or any other probe.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement
from netscope.probes import icmp_probe


def _classify_icmp_error(measurement: RawMeasurement) -> ProbeErrorType | None:
    """Best-effort classification of an ICMP RawMeasurement's existing
    free-text `error` string into a ProbeErrorType. Returns None for a
    successful measurement -- there is nothing to classify.

    Matches the exact error strings icmp_probe.py's ping() currently
    produces (unmodified by this task -- see that file):
    - "icmplib not installed" -> PROBE_UNAVAILABLE
    - any message mentioning "permission" (both the direct
      SocketPermissionError case and the "unprivileged fallback also
      failed" case) -> PERMISSION_DENIED
    - success=False with no `error` string at all -- icmp_probe.py
      does not set `error` when icmplib.ping() itself completes
      without raising but the host never responds (100% packet loss,
      is_alive=False) -- classified as TIMEOUT, since that is what a
      non-responding host over the probe's timeout window means.
    - anything else failed -> UNKNOWN, a deliberate, explicit fallback
      rather than guessing at an unrecognized message.
    """
    if measurement.success:
        return None

    if measurement.error is None:
        return ProbeErrorType.TIMEOUT

    error_text = measurement.error.lower()
    if "not installed" in error_text:
        return ProbeErrorType.PROBE_UNAVAILABLE
    if "permission" in error_text:
        return ProbeErrorType.PERMISSION_DENIED
    return ProbeErrorType.UNKNOWN


class ICMPProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping icmp_probe.ping()."""

    probe_type = ProbeType.ICMP

    def run(self, target: str, **options: Any) -> RawMeasurement:
        measurement = icmp_probe.ping(target, **options)
        measurement.error_type = _classify_icmp_error(measurement)
        return measurement
