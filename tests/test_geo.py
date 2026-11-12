"""
tests.test_geo

The v0.12 geographic domain layer: haversine distance, place/geo
models, staleness policy, and the canonical extra["geo"] shape.
Pure domain tests -- no database, no file I/O; the mmdb reader has
its own module (test_geoip_adapter.py).
"""

from __future__ import annotations

import pytest

from netscope.core.geo import (
    DB_STALE_AFTER_DAYS,
    EARTH_RADIUS_KM,
    GeoInfo,
    Place,
    geo_extra,
    haversine_km,
    place_distance_km,
)
from netscope.core.ports import GeoLookup

# Reference coordinates (decimal degrees, north/east positive).
TEHRAN = (35.6892, 51.3890)
LONDON = (51.5074, -0.1278)
PARIS = (48.8566, 2.3522)


class TestHaversine:
    def test_known_distance_tehran_to_london(self) -> None:
        # The distance the roadmap's motivating story runs on: a user
        # in Tehran checking a London-hosted target. Computed against
        # the same formula it asserts, pinned so any future radius or
        # formula change moves a named, reviewed number -- not a
        # silent drift.
        assert haversine_km(*TEHRAN, *LONDON) == pytest.approx(4396.57, abs=0.5)

    def test_known_distance_london_to_paris(self) -> None:
        assert haversine_km(*LONDON, *PARIS) == pytest.approx(343.56, abs=0.5)

    def test_same_point_is_zero(self) -> None:
        assert haversine_km(*TEHRAN, *TEHRAN) == pytest.approx(0.0, abs=1e-9)

    def test_half_the_equator_is_half_a_great_circle(self) -> None:
        # pi * R -- the largest distance a sphere can express between
        # two points. Catches sign/degree-vs-radian bugs the small
        # distances above can hide.
        assert haversine_km(0, 0, 0, 180) == pytest.approx(3.14159265 * EARTH_RADIUS_KM, rel=1e-6)

    def test_symmetric(self) -> None:
        assert haversine_km(*TEHRAN, *LONDON) == pytest.approx(haversine_km(*LONDON, *TEHRAN), rel=1e-12)

    def test_southern_and_western_negatives(self) -> None:
        # GeoLite2 reports south/west as negative degrees; distance
        # must not care which hemisphere order it receives.
        sydney = (-33.8688, 151.2093)
        santiago = (-33.4489, -70.6693)
        assert haversine_km(*sydney, *santiago) == pytest.approx(11355.0, rel=0.01)


class TestPlaceDistance:
    def test_places_with_coordinates(self) -> None:
        a = Place(latitude=TEHRAN[0], longitude=TEHRAN[1])
        b = Place(latitude=LONDON[0], longitude=LONDON[1])
        assert place_distance_km(a, b) == pytest.approx(4396.57, abs=0.5)

    def test_missing_coordinates_on_either_side_is_none(self) -> None:
        named = Place(country="Germany")
        located = Place(latitude=TEHRAN[0], longitude=TEHRAN[1])
        assert place_distance_km(named, located) is None
        assert place_distance_km(located, named) is None

    def test_none_places_are_none(self) -> None:
        located = Place(latitude=1.0, longitude=2.0)
        assert place_distance_km(None, located) is None
        assert place_distance_km(located, None) is None
        assert place_distance_km(None, None) is None


class TestPlaceLabel:
    def test_city_and_country(self) -> None:
        assert Place(city="Frankfurt", country="Germany").label() == "Frankfurt, Germany"

    def test_country_only(self) -> None:
        assert Place(country="Iran").label() == "Iran"

    def test_nothing_named_is_none(self) -> None:
        # Coordinates-only records have no label -- the UI shows the
        # distance alone, never a fabricated name.
        assert Place(latitude=51.5, longitude=-0.1).label() is None


class TestStaleness:
    def test_boundary_is_exclusive(self) -> None:
        info = GeoInfo(ip_address="1.2.3.4", db_age_days=DB_STALE_AFTER_DAYS)
        assert info.stale_db is False
        info = GeoInfo(ip_address="1.2.3.4", db_age_days=DB_STALE_AFTER_DAYS + 1)
        assert info.stale_db is True

    def test_unknown_age_is_not_stale(self) -> None:
        # Absence of metadata is absence of evidence, not evidence of
        # age -- a database that does not disclose its build date gets
        # no aging notice.
        assert GeoInfo(ip_address="1.2.3.4", db_age_days=None).stale_db is False

    def test_fresh_database(self) -> None:
        assert GeoInfo(ip_address="1.2.3.4", db_age_days=3).stale_db is False


class TestGeoExtra:
    def test_canonical_shape_is_flat_json_scalars(self) -> None:
        geo = GeoInfo(
            ip_address="81.2.69.160",
            place=Place(country="United Kingdom", country_code="GB", city="London", latitude=51.5142, longitude=-0.0931),
            distance_km=4396.57,
            db_type="GeoLite2-City",
            db_age_days=2,
        )
        extra = geo_extra(geo)
        # Every value must survive json.dumps unchanged -- this dict
        # rides inside extra_json and the --json payload verbatim.
        for value in extra.values():
            assert value is None or isinstance(value, (str, int, float, bool))
        assert extra["ip"] == "81.2.69.160"
        assert extra["country"] == "United Kingdom"
        assert extra["country_code"] == "GB"
        assert extra["city"] == "London"
        assert extra["distance_km"] == pytest.approx(4396.57)
        assert extra["db"] == "GeoLite2-City"
        assert extra["db_age_days"] == 2
        assert extra["stale_db"] is False

    def test_placeless_lookup_still_serializes(self) -> None:
        extra = geo_extra(GeoInfo(ip_address="1.2.3.4"))
        assert extra["country"] is None
        assert extra["city"] is None
        assert extra["distance_km"] is None
        assert extra["stale_db"] is False


class TestPort:
    def test_adapter_satisfies_protocol_structurally(self) -> None:
        # The hexagonal rule the other ports' tests pin: a fake with
        # the right shape IS a GeoLookup, without importing or
        # subclassing anything from core.
        class FakeLookup:
            def lookup(self, ip_address: str) -> GeoInfo | None:
                return None

        assert isinstance(FakeLookup(), GeoLookup)
