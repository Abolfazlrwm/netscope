"""
netscope.app.gateway

Gateway-resolution policy for the `diagnose` pipeline: turn the CLI's
`--gateway auto|<ip>|none` argument (or its absence) into the concrete
address -- or the deliberate absence of one -- that a measurement round
should ICMP-probe.

WHY THIS LIVES IN app
---------------------
This module composes a concrete adapter (`adapters.discovery.
gateway_detector.GatewayDetector`) with configuration (`NetScopeConfig`)
-- exactly the app-layer role ADR-002 assigns this package ("the one
package allowed to import both a core port and a concrete
netscope.adapters.* class together"). It makes no measurement decision
itself: whether the gateway is up, slow, or unreachable is still
entirely the probe/scoring/diagnosis layers' business.

RESOLUTION ORDER
----------------
1. `"none"` -> None. An explicit opt-OUT: no gateway measurement at
   all, regardless of config or what the OS reports.
2. Any other non-empty string (an IP or hostname) -> used verbatim.
3. `"auto"` (the CLI default) or None/absent -> the configured
   `config.gateway` if set, else a best-effort OS lookup
   (`GatewayDetector`). If neither yields an address, the result is
   None -- meaning "gateway not measured this round", which the
   diagnosis engine already treats honestly as insufficient evidence
   rather than silently assuming a healthy local network.
"""

from __future__ import annotations

from netscope.adapters.discovery.gateway_detector import GatewayDetector
from netscope.app.config import NetScopeConfig

NONE_SENTINEL = "none"
AUTO_SENTINEL = "auto"

_detector = GatewayDetector()


def resolve_gateway(gateway_arg: str | None, config: NetScopeConfig) -> str | None:
    """Resolve the effective gateway target for one measurement round.

    See the module docstring for the full resolution order. Never
    raises: an undetectable gateway resolves to None.
    """
    if gateway_arg is not None:
        arg = gateway_arg.strip()
        if arg.lower() == NONE_SENTINEL:
            return None
        if arg.lower() != AUTO_SENTINEL and arg != "":
            return arg

    if config.gateway:
        return config.gateway
    return _detector.detect()
