"""
netscope.core.geo

Geographic enrichment policy (v0.12, TASK-023 "Distance estimation"):
where a target lives, and how far from home it sits -- the last
unimplemented promise of the route model, whose `RouteHop.country`
field has documented itself as lookup-ready since TASK-020.

WHAT THIS MODULE OWNS
---------------------
- `Place`: a location -- country, city, coordinates -- as GeoIP data
  reports it. Plain data, no behavior beyond a display label.
- `GeoInfo`: one IP's geography as a lookup produced it, plus the
  bookkeeping a UI needs to disclose that lookup honestly (which
  database, how old).
- `haversine_km` / `place_distance_km`: great-circle distance. The
  haversine formula on the IUGG mean Earth radius -- accurate to
  ~0.5% for terrestrial distances, which is exactly the claim this
  feature makes ("approximately 4,400 km away", never "4,396.6 km").
- `DB_STALE_AFTER_DAYS` / `GeoInfo.stale_db`: the staleness policy.
  GeoLite2 is a snapshot of a moving world; the GeoLite2 EULA asks
  users to refresh regularly. A lookup from a database older than 30
  days is not wrong, but it is *aging evidence*, and the UI must say
  so -- the same honesty discipline that v0.9 applied to partial
  loss ("confirmed, not suspected") applied here to geography.
- `geo_extra`: the canonical `RawMeasurement.extra["geo"]` shape.
  One builder, so every surface that WRITES geo (the app-layer
  enrichment) and every surface that READS it (rich render, the HTML
  report, the --json payload's extra block) shares one key set.

WHY THIS LIVES IN CORE
----------------------
Distance and staleness are pure domain policy over domain types -- no
sockets, no file I/O, no mmdb byte parsing (the same split
core/loss.py draws: the confirm-then-report policy is core's, running
the extra sweeps is app's). Reading an actual MaxMind DB file is
infrastructure and lives in `adapters/geoip.py`, behind the
`GeoLookup` port in `core/ports.py`.

WHAT THIS MODULE DOES NOT DO
-----------------------------
- It does not resolve hostnames to IPs -- enrichment asks for an IP
  and gets one or nothing; the target-string-to-IP step lives with the
  adapter (it may consult the network).
- It does not decide *where the user is* -- `home` arrives resolved,
  from config (explicit coordinates or a home IP looked up in the same
  database). Guessing a user's location would be fabrication, and a
  wrong "distance from home" is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

# IUGG mean Earth radius, km. Not WGS84's equatorial 6378.137 -- the
# mean radius is the right sphere for a ~0.5%-accurate great-circle
# claim, and precision theater (ellipsoidal Vincenty) would suggest a
# rigor the GeoLite2 coordinates themselves do not carry.
EARTH_RADIUS_KM = 6371.0088

# A GeoLite2 database older than this renders with an aging notice
# (rich detail, HTML geography table) and stamps `stale_db` in the
# --json extra block. 30 days: the EULA's own "monthly" refresh rhythm,
# rounded to the way a user actually installs (beginning of a month).
DB_STALE_AFTER_DAYS = 30


@dataclass
class Place:
    """One location as GeoIP data reports it. Every field is optional:
    GeoLite2 records can carry a country with no city (or coordinates
    with no names), and honesty means representing the record that
    exists, not back-filling a half-empty one."""

    country: str | None = None
    country_code: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    def label(self) -> str | None:
        """Human display: "Frankfurt, Germany" -- the most specific
        parts that exist, most specific first. None when the record
        named nothing at all (coordinates-only places have no label;
        the UI shows the distance alone, never a placeholder name)."""
        parts = [part for part in (self.city, self.country) if part]
        return ", ".join(parts) if parts else None


@dataclass
class GeoInfo:
    """One IP's geography, as one lookup produced it.

    `distance_km` is populated only when BOTH sides of the question
    exist: the target has coordinates AND the user's home is known
    (config-supplied coordinates, or a home IP resolvable in the same
    database). `distance_km=None` with a populated place is a normal,
    honest result: "I know where it is; I don't know where you are."
    """

    ip_address: str
    place: Place | None = None
    distance_km: float | None = None
    db_type: str | None = None
    db_age_days: int | None = None

    @property
    def stale_db(self) -> bool:
        """Whether the database this answer came from has aged past the
        refresh rhythm the EULA asks for. None age (unknown metadata)
        is not stale -- absence of evidence is not evidence of age."""
        return self.db_age_days is not None and self.db_age_days > DB_STALE_AFTER_DAYS


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two coordinate pairs,
    the classic haversine on a spherical Earth of EARTH_RADIUS_KM.

    Pure and symmetric: haversine_km(a) == haversine_km(b, a). Inputs
    are decimal degrees (north/east positive, matching GeoLite2's
    location fields, which report south/west as negative).
    """
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi, dlambda = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def place_distance_km(a: Place | None, b: Place | None) -> float | None:
    """Distance between two places' coordinates, or None when either
    side lacks them. The None cases are normal GeoLite2 realities --
    a country-level record, a home configured by IP into a
    coordinates-less record -- and each returns None rather than
    0.0 or a fabricated number: an uncomputable distance is disclosed
    as absent, never rounded into existence."""
    if a is None or b is None:
        return None
    if a.latitude is None or a.longitude is None or b.latitude is None or b.longitude is None:
        return None
    return haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)


def geo_extra(geo: GeoInfo) -> dict:
    """The canonical `RawMeasurement.extra["geo"]` dict -- flat JSON
    scalars only, so it survives persistence (extra_json), the --json
    payload's `_json_safe` pass, and the HTML report's per-target
    geography table without any serializer special-casing it.

    Key set is stable API from v0.12: scripts that read
    `extra["geo"]["distance_km"]` read the documented shape, not a
    happy accident of one release's dict literal.
    """
    place = geo.place
    return {
        "ip": geo.ip_address,
        "country": place.country if place is not None else None,
        "country_code": place.country_code if place is not None else None,
        "city": place.city if place is not None else None,
        "latitude": place.latitude if place is not None else None,
        "longitude": place.longitude if place is not None else None,
        "distance_km": geo.distance_km,
        "db": geo.db_type,
        "db_age_days": geo.db_age_days,
        "stale_db": geo.stale_db,
    }
