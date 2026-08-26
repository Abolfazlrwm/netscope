"""
Adapter implementing netscope.core.discovery.DiscoveryProvider using
psutil (BSD-3-Clause) -- see adr-010-network-discovery.md for why
psutil, previously an unused dependency (per implementation-audit.md),
is used here.

Uses psutil.net_if_addrs() and psutil.net_if_stats() to enumerate local
interfaces and their up/down state, and translates the result into
NetworkInterface/NetworkSnapshot -- the existing domain models. Contains
no probing/measurement logic of its own, and (like the probe adapters
in adapters/probes/) is a thin translation layer, not a reimplementation
of what psutil already does. Interface-name-based network type
classification (TASK-012) is delegated entirely to the isolated, pure
network_type_classifier module, not implemented here.

discover_context() (TASK-013) is a thin, additive wrapper assembling
the result of discover() into a NetworkContext -- see NetworkContext's
own docstring in core/models.py for why it exists as a separate type.
"""

from __future__ import annotations

from netscope.adapters.discovery.network_type_classifier import classify_network_type
from netscope.core.discovery import DiscoveryProvider
from netscope.core.models import NetworkContext, NetworkInterface, NetworkSnapshot, utcnow

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


class PsutilNetworkDiscovery:
    """Satisfies netscope.core.discovery.DiscoveryProvider by wrapping
    psutil.net_if_addrs() / psutil.net_if_stats()."""

    def discover(self) -> NetworkSnapshot:
        if not _PSUTIL_AVAILABLE:
            # Defined, explicit empty-result behavior rather than raising --
            # mirrors icmp_probe.py's pattern of returning a well-formed,
            # empty/unsuccessful result when its library isn't installed,
            # instead of letting an ImportError propagate to the caller.
            return NetworkSnapshot(timestamp=utcnow(), interfaces=[])

        addrs_by_interface = psutil.net_if_addrs()
        stats_by_interface = psutil.net_if_stats()

        interfaces: list[NetworkInterface] = []
        for name, addr_list in addrs_by_interface.items():
            stats = stats_by_interface.get(name)
            is_up = stats.isup if stats is not None else False

            # AF_LINK entries are MAC/link-layer addresses, not IP
            # addresses -- excluded here since NetworkInterface.addresses
            # represents IP addresses. psutil.AF_LINK is used (rather
            # than importing the stdlib `socket` module for AF_INET/
            # AF_INET6) to keep this adapter's family-filtering logic
            # working directly off what psutil already reports.
            addresses = [a.address for a in addr_list if a.family != psutil.AF_LINK and a.address]

            interfaces.append(
                NetworkInterface(
                    name=name,
                    is_up=is_up,
                    addresses=addresses,
                    is_loopback="loopback" in (stats.flags if stats else "").split(","),
                    network_type=classify_network_type(name),
                )
            )

        return NetworkSnapshot(timestamp=utcnow(), interfaces=interfaces)

    def discover_context(self) -> NetworkContext:
        """Additive convenience method (TASK-013, "Network metadata"):
        assembles this adapter's NetworkSnapshot into a NetworkContext.

        discover() itself is completely unchanged by this method's
        addition -- this simply calls it and wraps the result, so
        existing callers of discover() (and the DiscoveryProvider
        Protocol in core/discovery.py, not modified by this task) keep
        working exactly as before. This method is additive surface, not
        a replacement.
        """
        return NetworkContext.from_snapshot(self.discover())
