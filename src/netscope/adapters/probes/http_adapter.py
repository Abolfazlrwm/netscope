"""
Adapter making the existing netscope.probes.http_probe.fetch() function
satisfy netscope.core.ports.Probe.

Contains no measurement logic of its own -- this adapter class does not
duplicate or reimplement http_probe.py's HTTP logic. This class only
translates the Probe Protocol's run(target, **options) call shape into
a call to the existing fetch(url, timeout) function (target maps to
url, the function's first positional parameter), and returns whatever
it returns, unchanged.

TASK-018 note: http_probe.py itself WAS modified by TASK-018 (TTFB
semantics fix, structured error classification) -- mirroring the
TCP/TLS pattern (TASK-016/017) rather than the ICMP/DNS retrofit
pattern (TASK-014/015), since HTTP's exception types are cleanly
available at the point they're caught, with no legacy string-only
constraint preventing classification at the source. This adapter
remains a pure pass-through regardless -- it has no classification
logic of its own to add either way.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeType, RawMeasurement
from netscope.probes import http_probe


class HTTPProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping http_probe.fetch()."""

    probe_type = ProbeType.HTTP

    def run(self, target: str, **options: Any) -> RawMeasurement:
        return http_probe.fetch(target, **options)
