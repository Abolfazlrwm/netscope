"""
netscope.app.container

Minimal composition root. Constructs concrete dependencies (for now:
just the ProbeRegistry) and exposes them to the rest of the application
layer, so that ui/ and future app use cases never need to construct
ICMPProbeAdapter/DNSProbeAdapter/HTTPProbeAdapter (or any other concrete
adapter class) themselves.

This is a plain, explicit factory -- there is no dependency-injection
framework here, no auto-wiring, no configuration-driven container.
Per docs/architecture/architecture-overview.md's own principle of
avoiding unnecessary abstraction, and per this task's explicit
instruction not to build a framework, "composition root" here means
exactly what it says: one place, one function, that builds the object
graph and hands it back.

netscope.app is the one package allowed to import both core.ports and
a concrete netscope.adapters.* class together (adr-002-probe-adapter-
strategy.md); this module is where that import actually happens for
probes.
"""

from __future__ import annotations

from dataclasses import dataclass

from netscope.adapters.probes.registry import ProbeRegistry


@dataclass
class Container:
    """Holds the application's constructed dependencies.

    For this task, that's only a ProbeRegistry -- persistence
    repositories, discovery adapters, and other dependencies are added
    here by their own later, separately-scoped tasks (future-roadmap.md
    TASK-010 onward) as those pieces are implemented. Nothing about this
    task's scope requires them yet.
    """

    probe_registry: ProbeRegistry


def build_container() -> Container:
    """Construct the default, production Container, wiring the real
    (non-fake) probe adapters via ProbeRegistry's own defaults.

    Tests that need a different set of probes (e.g. fakes) construct
    their own ProbeRegistry and pass it to Container directly, rather
    than calling this function -- build_container() is specifically the
    "real" wiring, analogous to a composition root's usual role.
    """
    return Container(probe_registry=ProbeRegistry())
