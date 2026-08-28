"""
Adapter making netscope.probes.tcp_probe.connect() satisfy
netscope.core.ports.Probe.

Unlike the ICMP/DNS adapters (which classify errors from pre-existing
free-text strings, since icmp_probe.py/dns_probe.py predate structured
errors and only ever emitted `error: str`), TCP has no legacy
implementation to preserve unmodified. tcp_probe.py itself classifies
errors directly from the real socket exception types it catches
(TimeoutError/socket.timeout, ConnectionRefusedError, PermissionError,
generic OSError), setting `error_type` at the exact point where the
real exception is available -- never brittle string matching. Because
of that, this adapter is a pure pass-through, with no classification
logic of its own to add.

TARGET/OPTIONS CONVENTION (TASK-016 decision, documented here since no
existing probe covers a two-part host+port target):

    run(target: str, *, port: int, timeout: float = 2.0) -> RawMeasurement

`target` is the hostname/IP -- matching every other probe's convention
that `target` is "the thing being probed". `port` is required via
**options rather than given a default, since guessing a service port
would be presumptuous (unlike ICMP/DNS/HTTP, TCP has no single implied
port the way HTTP implies 80/443 via its URL scheme). `timeout`
defaults to 2.0 seconds, matching ICMP/DNS's existing default. Calling
without `port` raises Python's normal TypeError for a missing required
argument, exactly as any other probe would behave if called with
missing required parameters -- no special-case handling was added for
this, consistent with "do not create a large configuration system".
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeType, RawMeasurement
from netscope.probes import tcp_probe


class TCPProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping tcp_probe.connect()."""

    probe_type = ProbeType.TCP

    def run(self, target: str, **options: Any) -> RawMeasurement:
        return tcp_probe.connect(target, **options)
