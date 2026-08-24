"""
Adapter making the existing netscope.probes.http_probe.fetch() function
satisfy netscope.core.ports.Probe.

Contains no measurement logic of its own -- http_probe.py is not modified
and is not reimplemented here. This class only translates the Probe
Protocol's run(target, **options) call shape into a call to the existing
fetch(url, timeout) function (target maps to url, the function's first
positional parameter), and returns whatever it returns, unchanged.
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
