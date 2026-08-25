"""
netscope.app.container

Minimal composition root. Constructs concrete dependencies (ProbeRegistry,
and now a DiscoveryProvider) and exposes them to the rest of the
application layer, so that ui/ and future app use cases never need to
construct ICMPProbeAdapter/DNSProbeAdapter/HTTPProbeAdapter/
PsutilNetworkDiscovery (or any other concrete adapter class) themselves.

This is a plain, explicit factory -- there is no dependency-injection
framework here, no auto-wiring, no configuration-driven container.
Per docs/architecture/architecture-overview.md's own principle of
avoiding unnecessary abstraction, and per this task's explicit
instruction not to build a framework, "composition root" here means
exactly what it says: one place, one function, that builds the object
graph and hands it back.

netscope.app is the one package allowed to import both a core port and
a concrete netscope.adapters.* class together (adr-002-probe-adapter-
strategy.md); this module is where that import actually happens, for
both probes and discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from netscope.adapters.discovery.network_discovery import PsutilNetworkDiscovery
from netscope.adapters.probes.registry import ProbeRegistry
from netscope.core.discovery import DiscoveryProvider


@dataclass
class Container:
    """Holds the application's constructed dependencies.

    discovery_provider defaults to the real PsutilNetworkDiscovery so
    that existing callers constructing Container(probe_registry=...)
    (e.g. tests/test_probe_registry.py, written before this task) keep
    working unchanged -- adding a required second field would have
    broken them, which this task's own discipline (preserve existing
    behavior, don't modify unrelated tests) rules out.

    Persistence repositories and other dependencies are added here by
    their own later, separately-scoped tasks (future-roadmap.md
    TASK-029 onward) as those pieces are implemented.
    """

    probe_registry: ProbeRegistry
    discovery_provider: DiscoveryProvider = field(default_factory=PsutilNetworkDiscovery)


def build_container() -> Container:
    """Construct the default, production Container, wiring the real
    (non-fake) probe adapters and discovery provider.

    Tests that need different dependencies (e.g. fakes) construct their
    own ProbeRegistry/DiscoveryProvider and pass them to Container
    directly, rather than calling this function -- build_container() is
    specifically the "real" wiring, analogous to a composition root's
    usual role.
    """
    return Container(
        probe_registry=ProbeRegistry(),
        discovery_provider=PsutilNetworkDiscovery(),
    )
