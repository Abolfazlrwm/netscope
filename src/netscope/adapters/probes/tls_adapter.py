"""
Adapter making netscope.probes.tls_probe.handshake() satisfy
netscope.core.ports.Probe.

Mirrors adapters/probes/tcp_adapter.py: TLS has no legacy implementation
to preserve, so tls_probe.py itself classifies errors directly from the
real ssl/socket exception types it catches -- this adapter is a pure
pass-through, with no classification logic of its own.

TARGET/OPTIONS CONVENTION: same shape as TCP's adapter --
run(target: str, *, port: int, timeout: float = 2.0). `target` is the
hostname (also used as the TLS server_hostname for SNI/certificate
verification, see tls_probe.py); `port` is required, no default, for
the same reasons documented in tcp_adapter.py.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeType, RawMeasurement
from netscope.probes import tls_probe


class TLSProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping tls_probe.handshake()."""

    probe_type = ProbeType.TLS

    def run(self, target: str, **options: Any) -> RawMeasurement:
        return tls_probe.handshake(target, **options)
