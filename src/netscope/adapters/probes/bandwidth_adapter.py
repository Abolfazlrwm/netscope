"""
Adapter making the existing netscope.probes.bandwidth_probe.measure()
function satisfy netscope.core.ports.Probe.

Contains no measurement logic of its own -- this adapter class does not
duplicate or reimplement bandwidth_probe.py's download logic. This class
only translates the Probe Protocol's run(target, **options) call shape
into a call to the existing measure(url, **options) function (target
maps to url, the function's first positional parameter), and returns
whatever it returns, unchanged -- the same pure pass-through pattern
every other adapter in this package follows.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeType, RawMeasurement
from netscope.probes import bandwidth_probe


class BandwidthProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping bandwidth_probe.measure()."""

    probe_type = ProbeType.BANDWIDTH

    def run(self, target: str, **options: Any) -> RawMeasurement:
        return bandwidth_probe.measure(target, **options)
