"""
Adapter making the existing netscope.probes.icmp_probe.ping() function
satisfy netscope.core.ports.Probe.

Contains no measurement logic of its own -- icmp_probe.py is not modified
and is not reimplemented here. This class only translates the Probe
Protocol's run(target, **options) call shape into a call to the existing
ping(target, count, timeout, privileged) function, and returns whatever
it returns, unchanged.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeType, RawMeasurement
from netscope.probes import icmp_probe


class ICMPProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping icmp_probe.ping()."""

    probe_type = ProbeType.ICMP

    def run(self, target: str, **options: Any) -> RawMeasurement:
        return icmp_probe.ping(target, **options)
