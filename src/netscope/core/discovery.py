"""
netscope.core.discovery

Port (contract) for network discovery providers -- the counterpart of
core.ports.Probe, but for "what's available to measure with" rather
than "run one measurement."

WHY DISCOVERY BELONGS OUTSIDE CORE
------------------------------------
Enumerating network interfaces requires talking to the OS (via psutil,
or platform-specific APIs). That is infrastructure, exactly like ICMP/
DNS/HTTP probing is -- and for the same reason core.ports.Probe
describes probes without importing icmplib/dnspython/httpx, this module
describes discovery without importing psutil, socket, or any
platform-specific module. See adr-010-network-discovery.md for the
full reasoning; this module's own regression test
(tests/test_network_discovery.py) enforces the import restriction
directly via AST inspection, the same pattern already used for
core/ports.py and adapters/probes/registry.py.

This module imports nothing beyond typing and netscope.core.models.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from netscope.core.models import NetworkSnapshot


@runtime_checkable
class DiscoveryProvider(Protocol):
    """Contract a discovery adapter must satisfy.

    A single method, `discover()`, taking no arguments and returning a
    NetworkSnapshot -- the existing domain model, reused as-is. Unlike
    Probe, discovery has no "target" concept: it reports on the local
    machine's own network state, not a remote host, so there is nothing
    for a caller to parameterize beyond "discover now."
    """

    def discover(self) -> NetworkSnapshot:
        ...
