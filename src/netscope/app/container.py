"""
netscope.app.container

Minimal composition root. Constructs concrete dependencies (ProbeRegistry,
DiscoveryProvider, MeasurementRepository, BaselineRepository,
ServiceRepository, RouteRepository, and the optional route-enrichment
lookups) and exposes them to the rest of the application layer, so that
ui/ and app use cases never need to construct ICMPProbeAdapter/
DNSProbeAdapter/HTTPProbeAdapter/PsutilNetworkDiscovery/MeasurementRepository
(or any other concrete adapter/persistence class) themselves.

This is a plain, explicit factory -- there is no dependency-injection
framework here, no auto-wiring, no configuration-driven container.
Per docs/architecture/architecture-overview.md's own principle of
avoiding unnecessary abstraction, "composition root" here means exactly
what it says: one place, one function, that builds the object graph and
hands it back.

netscope.app is the one package allowed to import both a core port and
a concrete netscope.adapters.* class together (adr-002-probe-adapter-
strategy.md); this module is where that import actually happens.

ROUTE ENRICHMENT (v0.2)
-----------------------
`asn_lookup` is wired only when an ASN database file exists at
`~/.netscope/ipasn.dat` (the documented, license-clean, offline data
source -- see adapters/lookups.py for the format). Without the file the
field stays None and route analysis degrades gracefully: hops render
without AS numbers, and `netscope route` says so. A missing database is
the normal state, never an error.

`hostname_resolver` is always wired: reverse-DNS is optional per call
(the `route` command's `--no-resolve` / TUI toggle), not per install.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from netscope.adapters.discovery.hostname_resolver import HostnameResolver
from netscope.adapters.discovery.network_discovery import PsutilNetworkDiscovery
from netscope.adapters.lookups import AsnLookupAdapter
from netscope.adapters.probes.registry import ProbeRegistry
from netscope.core.baseline import UserBaseline
from netscope.core.discovery import DiscoveryProvider
from netscope.persistence.baseline_repository import BaselineRepository
from netscope.persistence.measurement_repository import MeasurementRepository
from netscope.persistence.route_repository import RouteRepository
from netscope.persistence.service_repository import ServiceRepository

# One logger per top-level package, per architecture-decisions.md's
# Logging decision -- configure_logging sets each of these, not just
# the root logger, so "netscope.adapters" etc. each resolve predictably
# regardless of what else is configured in the process.
_NETSCOPE_PACKAGES = ("netscope.core", "netscope.adapters", "netscope.persistence", "netscope.app", "netscope.ui")

DEFAULT_ASN_DB_PATH = Path.home() / ".netscope" / "ipasn.dat"


def _open_asn_lookup(db_path: Path = DEFAULT_ASN_DB_PATH) -> AsnLookupAdapter | None:
    """Wire the ASN lookup adapter iff its offline database exists.

    A missing/invalid database is a normal degradation path, not a
    failure: route output simply renders without AS numbers (the same
    "optional enrichment, honest absence" convention the RouteHop
    lookup fields have documented since TASK-020)."""
    if not db_path.exists():
        return None
    try:
        return AsnLookupAdapter(ipasn_file=str(db_path))
    except Exception:  # noqa: BLE001 -- a corrupt database must never block a route command
        return None


@dataclass
class Container:
    """Holds the application's constructed dependencies.

    Optional fields default to None rather than a default_factory that
    performs I/O: unlike PsutilNetworkDiscovery (whose constructor does
    no I/O), the repositories open SQLite connections and the ASN lookup
    reads a database file at construction. Bare `Container(probe_registry=...)`
    construction (as tests do) stays exactly as side-effect-free as it
    has always been; `build_container()` is where the real wiring
    happens.
    """

    probe_registry: ProbeRegistry
    discovery_provider: DiscoveryProvider = field(default_factory=PsutilNetworkDiscovery)
    measurement_repository: MeasurementRepository | None = None
    baseline_repository: BaselineRepository | None = None
    service_repository: ServiceRepository | None = None
    route_repository: RouteRepository | None = None
    asn_lookup: AsnLookupAdapter | None = None
    hostname_resolver: HostnameResolver | None = None

    def load_baseline(self) -> UserBaseline:
        """Load the persisted personal baseline, or a fresh empty one
        when no repository is wired (the pre-0.2 in-memory behavior)."""
        if self.baseline_repository is not None:
            return self.baseline_repository.load()
        return UserBaseline()


def build_container(
    *,
    with_baseline_persistence: bool = True,
    with_route_enrichment: bool = True,
) -> Container:
    """Construct the default, production Container, wiring the real
    (non-fake) probe adapters, discovery provider, and repositories.

    Tests that need different dependencies (e.g. fakes) construct their
    own Container directly rather than calling this function --
    build_container() is specifically the "real" wiring, analogous to a
    composition root's usual role.
    """
    return Container(
        probe_registry=ProbeRegistry(),
        discovery_provider=PsutilNetworkDiscovery(),
        measurement_repository=MeasurementRepository.open(),
        baseline_repository=BaselineRepository.open() if with_baseline_persistence else None,
        service_repository=ServiceRepository.open(),
        route_repository=RouteRepository.open(),
        asn_lookup=_open_asn_lookup() if with_route_enrichment else None,
        hostname_resolver=HostnameResolver() if with_route_enrichment else None,
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
