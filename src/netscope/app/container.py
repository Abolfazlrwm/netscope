"""
netscope.app.container

Minimal composition root. Constructs concrete dependencies (ProbeRegistry,
DiscoveryProvider, and now optionally a MeasurementRepository) and
exposes them to the rest of the application layer, so that ui/ and
app use cases never need to construct ICMPProbeAdapter/DNSProbeAdapter/
HTTPProbeAdapter/PsutilNetworkDiscovery/MeasurementRepository (or any
other concrete adapter/persistence class) themselves.

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
probes, discovery, and (TASK-035) persistence.

TASK-035 also adds `configure_logging` here: architecture-decisions.md's
"Logging" decision requires logging to be "configured once by app's
composition root (never by core, which shouldn't have side effects at
import time)" -- this is that one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from netscope.adapters.discovery.network_discovery import PsutilNetworkDiscovery
from netscope.adapters.probes.registry import ProbeRegistry
from netscope.core.discovery import DiscoveryProvider
from netscope.persistence.measurement_repository import MeasurementRepository

# One logger per top-level package, per architecture-decisions.md's
# Logging decision -- configure_logging sets each of these, not just
# the root logger, so "netscope.adapters" etc. each resolve predictably
# regardless of what else is configured in the process.
_NETSCOPE_PACKAGES = ("netscope.core", "netscope.adapters", "netscope.persistence", "netscope.app", "netscope.ui")


@dataclass
class Container:
    """Holds the application's constructed dependencies.

    discovery_provider defaults to the real PsutilNetworkDiscovery so
    that existing callers constructing Container(probe_registry=...)
    (e.g. tests/test_probe_registry.py, written before this task) keep
    working unchanged -- adding a required second field would have
    broken them, which this task's own discipline (preserve existing
    behavior, don't modify unrelated tests) rules out.

    measurement_repository (TASK-035) defaults to None rather than a
    default_factory that opens a real database file: unlike
    PsutilNetworkDiscovery (whose constructor does no I/O -- only
    calling its methods touches the OS), MeasurementRepository.open()
    immediately creates a directory and opens a SQLite connection. A
    default_factory would make every bare `Container(probe_registry=...)`
    construction in tests silently touch the real filesystem, breaking
    the "fully offline unit tests" principle
    (architecture-overview.md SS3's `app` test-strategy row) for tests
    that never asked for persistence at all. `build_container()` below
    is where the real one gets constructed; direct `Container(...)`
    construction (as tests already do) stays exactly as side-effect-free
    as it was before this task.

    Other persistence repositories (Route/Baseline/Incident) are added
    here by their own later, separately-scoped tasks as those pieces
    are implemented.
    """

    probe_registry: ProbeRegistry
    discovery_provider: DiscoveryProvider = field(default_factory=PsutilNetworkDiscovery)
    measurement_repository: Optional[MeasurementRepository] = None


def build_container() -> Container:
    """Construct the default, production Container, wiring the real
    (non-fake) probe adapters, discovery provider, and measurement
    repository.

    Tests that need different dependencies (e.g. fakes) construct their
    own ProbeRegistry/DiscoveryProvider/MeasurementRepository and pass
    them to Container directly, rather than calling this function --
    build_container() is specifically the "real" wiring, analogous to a
    composition root's usual role.
    """
    return Container(
        probe_registry=ProbeRegistry(),
        discovery_provider=PsutilNetworkDiscovery(),
        measurement_repository=MeasurementRepository.open(),
    )


def configure_logging(verbose: bool = False) -> None:
    """Configure NetScope's loggers -- one per top-level package, per
    architecture-decisions.md's Logging decision. Default WARNING,
    raised to INFO when `verbose` is set (the CLI's `--verbose`/`-v`
    flag). Called exactly once, by the composition root/CLI entry
    point -- never by `core`, which must stay side-effect-free at
    import time (same decision document).
    """
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    for package in _NETSCOPE_PACKAGES:
        logging.getLogger(package).setLevel(level)
