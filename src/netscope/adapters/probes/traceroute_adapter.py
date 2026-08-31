"""
Adapter making netscope.probes.traceroute_probe.traceroute() satisfy
netscope.core.ports.Probe.

Contains no measurement logic of its own -- this adapter class does not
duplicate or reimplement traceroute_probe.py's icmplib-wrapping logic.
This class only translates the Probe Protocol's run(target, **options)
call shape into a call to the existing traceroute(host, **options)
function, and returns whatever it returns, unchanged. Mirrors every
other adapter in this project (icmp_adapter.py, dns_adapter.py,
http_adapter.py, tcp_adapter.py, tls_adapter.py) -- measurement logic
lives in probes/, protocol-conformance translation lives here.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeType, RawMeasurement
from netscope.probes import traceroute_probe


class TracerouteProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping
    traceroute_probe.traceroute()."""

    probe_type = ProbeType.TRACEROUTE

    def run(self, target: str, **options: Any) -> RawMeasurement:
        return traceroute_probe.traceroute(target, **options)
