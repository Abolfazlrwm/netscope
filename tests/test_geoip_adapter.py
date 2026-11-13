"""
tests.test_geoip_adapter

The v0.12 mmdb reader and its two adapters (MaxMindGeoLookup for
GeoLite2-City, v0.14's MaxMindAsnLookup for GeoLite2-ASN), tested
entirely against synthetic fixture databases (tests/mmdb_fixture.py)
-- GeoLite2 is never downloaded, committed, or needed here.

Two extra layers of confidence, both opt-in and offline:

- `TestSpecCrosscheck` compares this reader against the OFFICIAL
  `maxminddb` reference library on the SAME fixture bytes. It skips
  wherever the reference library is not installed (CI does not
  install it -- it is deliberately not a project dependency), so the
  crosscheck runs for the developer who has it around and never
  gates anyone else.
- The fixture writer implements the real public format, so passing
  here is meaningful spec conformance, not a tautology: reader and
  writer agree because BOTH follow the spec, not because one was
  written to match the other's quirks.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from netscope.adapters.geoip import (
    GeoLookupUnavailableError,
    MaxMindAsnLookup,
    MaxMindGeoLookup,
    MMDBFormatError,
    MMDBReader,
    resolve_target_ip,
)
from netscope.core.asn import AsnInfo
from netscope.core.geo import DB_STALE_AFTER_DAYS, GeoInfo, Place
from netscope.core.models import utcnow
from netscope.core.ports import AsnLookup, GeoLookup
from tests.mmdb_fixture import build_mmdb

# Synthetic GeoLite2-City-shaped records. London/Paris/Tehran carry
# the same coordinates the haversine tests pin, so distance numbers
# agree across the two test modules.
LONDON_RECORD = {
    "city": {"geoname_id": 2643743, "names": {"de": "London", "en": "London"}},
    "country": {"geoname_id": 2635167, "iso_code": "GB", "names": {"de": "Vereinigtes Königreich", "en": "United Kingdom"}},
    "location": {"accuracy_radius": 100, "latitude": 51.5074, "longitude": -0.1278, "time_zone": "Europe/London"},
}
TEHRAN_RECORD = {
    "city": {"names": {"en": "Tehran"}},
    "country": {"iso_code": "IR", "names": {"en": "Iran"}},
    "location": {"latitude": 35.6892, "longitude": 51.3890},
}
MOUNTAIN_VIEW_RECORD = {
    "city": {"names": {"en": "Mountain View"}},
    "country": {"iso_code": "US", "names": {"en": "United States"}},
    "location": {"latitude": 37.386, "longitude": -122.0838},
}
COUNTRY_ONLY_RECORD = {
    # Country-level record: no city, no coordinates -- the shape a
    # GeoLite2-City lookup can honestly return for sparse blocks.
    "country": {"iso_code": "DE", "names": {"en": "Germany"}},
}

RECORDS = {
    "81.2.69.0/24": LONDON_RECORD,
    "5.35.90.0/24": TEHRAN_RECORD,
    "8.8.8.0/24": MOUNTAIN_VIEW_RECORD,
    "198.51.100.0/24": COUNTRY_ONLY_RECORD,
}


def write_db(
    tmp_path: Path,
    networks: dict[str, dict[str, object]] | None = None,
    *,
    build_epoch: int | None = None,
    record_size: int = 24,
    ip_version: int = 4,
) -> Path:
    path = tmp_path / "fixture.mmdb"
    if build_epoch is None:
        build_epoch = int(utcnow().timestamp()) - 2 * 86400  # two days old: fresh
    path.write_bytes(
        build_mmdb(
            RECORDS if networks is None else networks,
            build_epoch=build_epoch,
            record_size=record_size,
            ip_version=ip_version,
        )
    )
    return path


class TestReader:
    def test_lookup_within_network(self, tmp_path: Path) -> None:
        reader = MMDBReader(write_db(tmp_path))
        record = reader.get("81.2.69.160")  # inside /24, not the network address itself
        assert record is not None
        assert record["country"]["iso_code"] == "GB"
        assert record["city"]["names"]["en"] == "London"
        assert record["location"]["latitude"] == pytest.approx(51.5074)

    def test_unknown_address_is_none(self, tmp_path: Path) -> None:
        reader = MMDBReader(write_db(tmp_path))
        assert reader.get("203.0.113.7") is None
        assert reader.get("10.0.0.1") is None

    def test_metadata_roundtrip(self, tmp_path: Path) -> None:
        meta = MMDBReader(write_db(tmp_path)).metadata
        assert meta.database_type == "GeoLite2-City"
        assert meta.ip_version == 4
        assert meta.record_size == 24
        assert meta.node_count > 0
        assert isinstance(meta.build_epoch, int)

    @pytest.mark.parametrize("record_size", [24, 28, 32])
    def test_every_record_size(self, tmp_path: Path, record_size: int) -> None:
        # Readers must accept all three spec sizes; GeoLite2-City
        # ships 28, GeoLite2-Country 24, and 32 completes the set.
        reader = MMDBReader(write_db(tmp_path, record_size=record_size))
        record = reader.get("8.8.8.4")  # inside 8.8.8.0/24, not the network address itself
        assert record is not None
        assert record["city"]["names"]["en"] == "Mountain View"

    def test_ipv6_database_with_ipv4_mapped_networks(self, tmp_path: Path) -> None:
        # GeoLite2 databases are ip_version 6 trees that store IPv4
        # under ::/96 -- the zero-walk entry the official readers use.
        networks = {"81.2.69.0/24": LONDON_RECORD, "2001:db8::/32": MOUNTAIN_VIEW_RECORD}
        reader = MMDBReader(write_db(tmp_path, networks=networks, ip_version=6, record_size=28))
        assert reader.get("81.2.69.1") is not None
        v6 = reader.get("2001:db8::1")
        assert v6 is not None
        assert v6["country"]["iso_code"] == "US"
        assert reader.get("203.0.113.9") is None

    def test_pure_ipv6_address_in_ipv6_database(self, tmp_path: Path) -> None:
        networks = {"2001:db8:1234::/48": TEHRAN_RECORD}
        reader = MMDBReader(write_db(tmp_path, networks=networks, ip_version=6, record_size=32))
        record = reader.get("2001:db8:1234:ffff::1")
        assert record is not None
        assert record["city"]["names"]["en"] == "Tehran"
        assert reader.get("2001:db8:9999::1") is None

    def test_ipv6_address_against_ipv4_database_is_format_error(self, tmp_path: Path) -> None:
        reader = MMDBReader(write_db(tmp_path))
        with pytest.raises(MMDBFormatError):
            reader.get("2001:db8::1")

    def test_deep_prefix_needs_no_wasted_nodes(self, tmp_path: Path) -> None:
        # A /32 against a fresh tree builds exactly 32 nodes of path,
        # proving the tree branches on demand (a naive fixed-size
        # implementation would OOM on the fixture; a lazy one passes).
        networks = {"9.9.9.9/32": {"country": {"iso_code": "XX", "names": {"en": "Nowhere"}}}}
        meta = MMDBReader(write_db(tmp_path, networks=networks)).metadata
        assert meta.node_count == 32


class TestSpecCrosscheck:
    """This reader vs the official `maxminddb` library, same bytes.
    Skips when the reference library is not installed -- it is a
    validation convenience, never a dependency."""

    @pytest.fixture(scope="class")
    def official(self) -> object:
        pytest.importorskip("maxminddb", reason="official reference library not installed")
        import maxminddb

        return maxminddb

    def test_official_library_reads_our_fixture(self, tmp_path: Path, official: object) -> None:  # type: ignore[name-defined]
        path = write_db(tmp_path, record_size=28)
        reference = official.open_database(str(path))  # type: ignore[attr-defined]
        assert reference.get("81.2.69.160") == LONDON_RECORD
        assert reference.get("5.35.90.77") == TEHRAN_RECORD
        assert reference.get("203.0.113.1") is None

    def test_our_reader_matches_the_reference_on_every_network(self, tmp_path: Path, official: object) -> None:  # type: ignore[name-defined]
        path = write_db(tmp_path, record_size=28)
        reference = official.open_database(str(path))  # type: ignore[attr-defined]
        ours = MMDBReader(path)
        for address in ("81.2.69.160", "81.2.69.1", "8.8.8.8", "198.51.100.5", "203.0.113.9", "10.1.2.3"):
            assert ours.get(address) == reference.get(address), address


class TestAdapter:
    def test_satisfies_geo_port(self, tmp_path: Path) -> None:
        assert isinstance(MaxMindGeoLookup(write_db(tmp_path)), GeoLookup)

    def test_lookup_place_fields(self, tmp_path: Path) -> None:
        lookup = MaxMindGeoLookup(write_db(tmp_path))
        info = lookup.lookup("81.2.69.160")
        assert info is not None
        assert info.place is not None
        assert info.place.label() == "London, United Kingdom"
        assert info.place.country_code == "GB"
        assert info.db_type == "GeoLite2-City"

    def test_unknown_address_is_none(self, tmp_path: Path) -> None:
        assert MaxMindGeoLookup(write_db(tmp_path)).lookup("192.0.2.55") is None

    def test_invalid_address_is_valueerror(self, tmp_path: Path) -> None:
        # The port's documented split: invalid input is a caller
        # error, unknown input is a normal None.
        with pytest.raises(ValueError):
            MaxMindGeoLookup(write_db(tmp_path)).lookup("not-an-ip")

    def test_country_only_record_has_no_distance(self, tmp_path: Path) -> None:
        lookup = MaxMindGeoLookup(write_db(tmp_path), home=Place(latitude=35.6892, longitude=51.3890))
        info = lookup.lookup("198.51.100.9")
        assert info is not None
        assert info.place is not None
        assert info.place.label() == "Germany"
        assert info.distance_km is None

    def test_distance_from_home_coordinates(self, tmp_path: Path) -> None:
        lookup = MaxMindGeoLookup(write_db(tmp_path), home=Place(latitude=35.6892, longitude=51.3890))
        info = lookup.lookup("81.2.69.160")
        assert info is not None
        assert info.distance_km is not None
        assert info.distance_km == pytest.approx(4396.57, abs=0.5)  # Tehran -> London

    def test_no_home_no_distance(self, tmp_path: Path) -> None:
        info = MaxMindGeoLookup(write_db(tmp_path)).lookup("81.2.69.160")
        assert info is not None
        assert info.distance_km is None

    def test_db_age_days_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        now = utcnow()
        monkeypatch.setattr("netscope.adapters.geoip.utcnow", lambda: now)
        epoch = int(now.timestamp())
        lookup = MaxMindGeoLookup(write_db(tmp_path, build_epoch=epoch - 45 * 86400))
        info = lookup.lookup("81.2.69.160")
        assert info is not None
        assert info.db_age_days == 45
        assert info.stale_db is True  # past core.geo's 30-day EULA rhythm

    def test_fresh_database_not_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        now = utcnow()
        monkeypatch.setattr("netscope.adapters.geoip.utcnow", lambda: now)
        lookup = MaxMindGeoLookup(write_db(tmp_path, build_epoch=int(now.timestamp()) - DB_STALE_AFTER_DAYS * 86400))
        info = lookup.lookup("81.2.69.160")
        assert info is not None
        assert info.stale_db is False

    def test_corrupt_file_raises_unavailable(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.mmdb"
        path.write_bytes(b"\x00" * 512)
        with pytest.raises(GeoLookupUnavailableError):
            MaxMindGeoLookup(path)

    def test_missing_file_raises_unavailable(self, tmp_path: Path) -> None:
        with pytest.raises(GeoLookupUnavailableError):
            MaxMindGeoLookup(tmp_path / "nope.mmdb")

    def test_truncated_metadata_raises_unavailable(self, tmp_path: Path) -> None:
        path = tmp_path / "cut.mmdb"
        path.write_bytes(write_db(tmp_path).read_bytes()[:-10])
        with pytest.raises(GeoLookupUnavailableError):
            MaxMindGeoLookup(path)

    def test_empty_database_looks_up_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.mmdb"
        path.write_bytes(build_mmdb({}, build_epoch=1700000000))
        lookup = MaxMindGeoLookup(path)
        assert lookup.lookup("81.2.69.160") is None


# Synthetic GeoLite2-ASN-shaped records (v0.14): flat, no names map,
# no coordinates -- the shape the real edition's records keep.
ASN_RECORDS = {
    "81.2.69.0/24": {"autonomous_system_number": 13335, "autonomous_system_organization": "Cloudflare, Inc."},
    "5.35.90.0/24": {"autonomous_system_number": 58224},
    "8.8.8.0/24": {"autonomous_system_number": 15169, "autonomous_system_organization": "GOOGLE"},
}


def write_asn_db(
    tmp_path: Path,
    networks: dict[str, dict[str, object]] | None = None,
    *,
    build_epoch: int | None = None,
    record_size: int = 24,
) -> Path:
    path = tmp_path / "asn-fixture.mmdb"
    if build_epoch is None:
        build_epoch = int(utcnow().timestamp()) - 2 * 86400  # two days old: fresh
    path.write_bytes(
        build_mmdb(
            ASN_RECORDS if networks is None else networks,
            build_epoch=build_epoch,
            record_size=record_size,
            database_type="GeoLite2-ASN",
        )
    )
    return path


class TestAsnAdapter:
    def test_satisfies_asn_port(self, tmp_path: Path) -> None:
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path))
        assert isinstance(lookup, AsnLookup)

    def test_lookup_number_and_organization(self, tmp_path: Path) -> None:
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path))
        info = lookup.lookup("81.2.69.160")
        assert isinstance(info, AsnInfo)
        assert info.asn == 13335
        assert info.organization == "Cloudflare, Inc."
        assert info.ip_address == "81.2.69.160"

    def test_number_without_name_is_carried_bare(self, tmp_path: Path) -> None:
        # The record that names nothing: the number is real evidence,
        # the missing name is disclosed as None, never filled in.
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path))
        info = lookup.lookup("5.35.90.7")
        assert info is not None
        assert info.asn == 58224
        assert info.organization is None

    def test_unknown_address_is_none(self, tmp_path: Path) -> None:
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path))
        assert lookup.lookup("203.0.113.7") is None
        assert lookup.lookup("10.0.0.1") is None

    def test_invalid_address_is_valueerror(self, tmp_path: Path) -> None:
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path))
        with pytest.raises(ValueError):
            lookup.lookup("not-an-ip-address")

    def test_db_age_and_staleness_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real_dt = datetime
        old = real_dt.now() - timedelta(days=DB_STALE_AFTER_DAYS + 5)
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path, build_epoch=int(old.timestamp())))
        info = lookup.lookup("81.2.69.160")
        assert info is not None
        assert info.db_type == "GeoLite2-ASN"
        assert info.db_age_days is not None and info.db_age_days > DB_STALE_AFTER_DAYS
        assert info.stale_db is True

    def test_fresh_database_not_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        now = utcnow()
        monkeypatch.setattr("netscope.adapters.geoip.utcnow", lambda: now)
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path, build_epoch=int(now.timestamp()) - 2 * 86400))
        info = lookup.lookup("81.2.69.160")
        assert info is not None
        assert info.stale_db is False

    def test_record_without_a_number_is_none(self, tmp_path: Path) -> None:
        # An ownership claim with no AS behind it is not a claim this
        # adapter carries.
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path, {"81.2.69.0/24": {"autonomous_system_organization": "Ghost"}}))
        assert lookup.lookup("81.2.69.1") is None

    def test_corrupt_file_raises_unavailable(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.mmdb"
        path.write_bytes(b"\x00" * 512)
        with pytest.raises(GeoLookupUnavailableError):
            MaxMindAsnLookup(path)

    def test_missing_file_raises_unavailable(self, tmp_path: Path) -> None:
        with pytest.raises(GeoLookupUnavailableError):
            MaxMindAsnLookup(tmp_path / "nope.mmdb")

    @pytest.mark.parametrize("record_size", [24, 28, 32])
    def test_every_record_size(self, tmp_path: Path, record_size: int) -> None:
        lookup = MaxMindAsnLookup(write_asn_db(tmp_path, record_size=record_size))
        info = lookup.lookup("8.8.8.8")
        assert info is not None
        assert info.asn == 15169


class TestResolveTargetIp:
    def test_ip_literal_wins(self) -> None:
        assert resolve_target_ip("81.2.69.160") == "81.2.69.160"
        assert resolve_target_ip("2001:db8::1") == "2001:db8::1"

    def test_dns_answers_scanned_first(self) -> None:
        # The DNS probe already recorded the resolution -- enrichment
        # reuses it rather than asking the resolver again.
        extra = {"answers": ["93.184.216.34"], "resolver": "1.1.1.1"}
        assert resolve_target_ip("example.com", extra) == "93.184.216.34"

    def test_answer_tokens_parsed(self) -> None:
        # MX-shaped answers ("10 mail.example.com.") are scanned
        # token by token for anything IP-shaped.
        extra = {"answers": ["10 mail.example.com.", "93.184.216.34"]}
        assert resolve_target_ip("example.com", extra) == "93.184.216.34"

    def test_getaddrinfo_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "netscope.adapters.geoip.socket.getaddrinfo",
            lambda host, port: [(2, 1, 6, "", ("203.0.113.5", 0))],
        )
        assert resolve_target_ip("unresolved.example") == "203.0.113.5"

    def test_resolution_failure_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(host: str, port: int) -> list:
            raise OSError("no resolver")

        monkeypatch.setattr("netscope.adapters.geoip.socket.getaddrinfo", boom)
        assert resolve_target_ip("unresolved.example") is None

    def test_no_answers_no_resolver_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(host: str, port: int) -> list:
            raise OSError("no resolver")

        monkeypatch.setattr("netscope.adapters.geoip.socket.getaddrinfo", boom)
        assert resolve_target_ip("unresolved.example", {"answers": ["mail.example.com."]}) is None


class TestGeoInfoWire:
    def test_port_result_shape_is_core_owned(self, tmp_path: Path) -> None:
        # The adapter returns the core model, not its own record type:
        # enrichment and the UI consume GeoInfo, never a MaxMind dict.
        lookup = MaxMindGeoLookup(write_db(tmp_path), home=Place(latitude=35.6892, longitude=51.3890))
        info = lookup.lookup("81.2.69.160")
        assert isinstance(info, GeoInfo)
