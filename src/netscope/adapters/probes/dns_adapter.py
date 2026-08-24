"""
Adapter making the existing netscope.probes.dns_probe.resolve() function
satisfy netscope.core.ports.Probe.

Contains no measurement logic of its own -- dns_probe.py is not modified
and is not reimplemented here. This class only translates the Probe
Protocol's run(target, **options) call shape into a call to the existing
resolve(hostname, record_type, resolver_ip, timeout) function (target
maps to hostname, the function's first positional parameter), and
returns whatever it returns, unchanged.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeType, RawMeasurement
from netscope.probes import dns_probe


class DNSProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping dns_probe.resolve()."""

    probe_type = ProbeType.DNS

    def run(self, target: str, **options: Any) -> RawMeasurement:
        return dns_probe.resolve(target, **options)
