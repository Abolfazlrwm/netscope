"""
netscope.app.container

Minimal composition root. Constructs concrete dependencies (ProbeRegistry,
DiscoveryProvider, MeasurementRepository, BaselineRepository,
ServiceRepository, RouteRepository, and the optional route-enrichment
and geo lookups) and exposes them to the rest of the application layer, so
that ui/ and app use cases never need to construct ICMPProbeAdapter/
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

GEO ENRICHMENT (v0.12)
-----------------------
`geo_lookup` follows the same "optional enrichment, honest absence"
discipline: it is wired only when a MaxMind DB (GeoLite2-City) file
exists -- the user's own download, by default at
`~/.netscope/GeoLite2-City.mmdb`, or wherever `geo_db_path` in the
config points. The user's home place is resolved here, once, from the
config: explicit coordinates win, else `home_ip` is looked up in the
same database; with neither, lookups still answer with a target's
place and distance simply stays None -- the adapter never guesses
where the user is. A corrupt database is a degraded install (no geo
anywhere), not a crash: the wiring catches and reports nothing, the
same contract `_open_asn_lookup` established.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from netscope.adapters.discovery.hostname_resolver import HostnameResolver
from netscope.adapters.discovery.network_discovery import PsutilNetworkDiscovery
from netscope.adapters.geoip import GeoLookupUnavailableError, MaxMindGeoLookup, TargetIpResolver
from netscope.adapters.lookups import AsnLookupAdapter
from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.core.baseline import UserBaseline
from netscope.core.discovery import DiscoveryProvider
from netscope.core.geo import Place
from netscope.core.ports import GeoLookup
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
DEFAULT_GEO_DB_PATH = Path.home() / ".netscope" / "GeoLite2-City.mmdb"


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


def _open_geo_lookup(config: NetScopeConfig) -> GeoLookup | None:
    """Wire the GeoIP lookup adapter iff the user's GeoLite2 database
    exists (default path, or the config's `geo_db_path` override).

    Home resolution, once, at wiring time: explicit coordinates win
    over a `home_ip` lookup in the same database; neither configured
    means the adapter still answers with places and `distance_km`
    stays None -- honest absence beats a guessed home. A database
    that fails to open degrades to no-geo (never blocks a command):
    the same "corrupt optional database is a degraded install, not an
    error" contract `_open_asn_lookup` established."""
    db_path = Path(config.geo_db_path) if config.geo_db_path else DEFAULT_GEO_DB_PATH
    if not db_path.exists():
        return None
    try:
        lookup = MaxMindGeoLookup(db_path)
    except GeoLookupUnavailableError:
        return None

    home: Place | None = None
    if config.home_latitude is not None and config.home_longitude is not None:
        home = Place(latitude=float(config.home_latitude), longitude=float(config.home_longitude))
    elif config.home_ip:
        try:
            info = lookup.lookup(config.home_ip)
        except (ValueError, GeoLookupUnavailableError):
            info = None
        if info is not None and info.place is not None:
            home = info.place
    lookup.home = home
    return lookup


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
    geo_lookup: GeoLookup | None = None
    ip_resolver: TargetIpResolver | None = None

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
    with_geo_enrichment: bool = True,
    config: NetScopeConfig | None = None,
) -> Container:
    """Construct the default, production Container, wiring the real
    (non-fake) probe adapters, discovery provider, and repositories.

    Tests that need different dependencies (e.g. fakes) construct their
    own Container directly rather than calling this function --
    build_container() is specifically the "real" wiring, analogous to a
    composition root's usual role.

    `config` feeds the geo wiring (`geo_db_path`, the user's home as
    coordinates or `home_ip`). When it is not supplied -- the TUI's
    lazy build, tests -- built-in defaults apply, which point geo at
    the standard `~/.netscope/GeoLite2-City.mmdb` location and leave
    the home unknown; a command-line run always passes the config the
    CLI already loaded, so nothing is read from disk twice."""
    geo_config = config if config is not None else NetScopeConfig()
    return Container(
        probe_registry=ProbeRegistry(),
        discovery_provider=PsutilNetworkDiscovery(),
        measurement_repository=MeasurementRepository.open(),
        baseline_repository=BaselineRepository.open() if with_baseline_persistence else None,
        service_repository=ServiceRepository.open(),
        route_repository=RouteRepository.open(),
        asn_lookup=_open_asn_lookup() if with_route_enrichment else None,
        hostname_resolver=HostnameResolver() if with_route_enrichment else None,
        geo_lookup=_open_geo_lookup(geo_config) if with_geo_enrichment else None,
        ip_resolver=TargetIpResolver() if with_geo_enrichment else None,
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
