"""
tests.test_geo_enrichment

The v0.12 app-layer geo wiring: `enrich_with_geo` (measurement ->
extra["geo"]), the use-case call sites (`run_measurement_round`,
`check_service`), the route-hop country fill in `_enrich_snapshot`,
and the composition root's `_open_geo_lookup` wiring decisions.

Everything runs offline: the GeoLookup is a fake implementing the
core port, and the container-wiring tests use the synthetic mmdb
fixture from tests/mmdb_fixture.py -- the real GeoLite2 database is
never needed here (or anywhere in the suite).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from netscope.adapters.geoip import GeoLookupUnavailableError, TargetIpResolver
from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.config import NetScopeConfig
from netscope.app.container import Container, _open_geo_lookup, build_container
from netscope.app.use_cases import check_target, enrich_with_geo, run_measurement_round
from netscope.core.geo import DB_STALE_AFTER_DAYS, GeoInfo, Place
from netscope.core.models import ProbeType, RawMeasurement, RouteHop, RouteSnapshot
from netscope.core.ports import GeoLookup
from tests.mmdb_fixture import build_mmdb

LONDON_IP = "81.2.69.160"
TEHRAN = Place(city="Tehran", country="Iran", latitude=35.6892, longitude=51.3890)


@dataclass
class FakeGeoLookup:
    """A GeoLookup-port fake with a scripted answer table -- enough
    shape to satisfy the runtime_checkable protocol and to drive every
    enrichment branch (hit, miss, error)."""

    answers: dict[str, GeoInfo] = field(default_factory=dict)
    raise_on: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def lookup(self, ip_address: str) -> GeoInfo | None:
        self.calls.append(ip_address)
        if ip_address in self.raise_on:
            raise GeoLookupUnavailableError("scripted failure")
        return self.answers.get(ip_address)


def _measurement(target: str = "example.com", **extra) -> RawMeasurement:
    return RawMeasurement(probe_type=ProbeType.DNS, target=target, success=True, latency_ms=12.0, extra={"answers": [LONDON_IP], **extra})


class TestEnrichWithGeo:
    def test_attaches_canonical_geo_extra(self) -> None:
        container = Container(
            probe_registry=ProbeRegistry(),
            geo_lookup=FakeGeoLookup(
                answers={
                    LONDON_IP: GeoInfo(
                        ip_address=LONDON_IP,
                        place=Place(city="London", country="United Kingdom", latitude=51.5074, longitude=-0.1278),
                        db_type="GeoLite2-City",
                        db_age_days=2,
                    )
                }
            ),
            ip_resolver=TargetIpResolver(),
        )
        m = _measurement()
        enrich_with_geo([m], container)
        geo = m.extra["geo"]
        assert geo["ip"] == LONDON_IP
        assert geo["country"] == "United Kingdom"
        assert geo["city"] == "London"
        assert geo["distance_km"] is None  # no home configured -> no distance, honestly
        assert geo["db"] == "GeoLite2-City"
        assert geo["stale_db"] is False

    def test_no_lookup_wired_is_noop(self) -> None:
        m = _measurement()
        enrich_with_geo([m], Container(probe_registry=ProbeRegistry()))
        assert "geo" not in m.extra

    def test_unknown_address_is_silently_skipped(self) -> None:
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=FakeGeoLookup())
        m = _measurement()
        enrich_with_geo([m], container)
        assert "geo" not in m.extra

    def test_unavailable_database_is_silently_skipped(self) -> None:
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=FakeGeoLookup(raise_on={LONDON_IP}))
        m = _measurement()
        enrich_with_geo([m], container)
        assert "geo" not in m.extra

    def test_already_tagged_is_never_re_enriched(self) -> None:
        lookup = FakeGeoLookup(answers={LONDON_IP: GeoInfo(ip_address=LONDON_IP, place=Place(country="United Kingdom"))})
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=lookup, ip_resolver=TargetIpResolver())
        m = _measurement()
        m.extra["geo"] = {"country": "pinned-by-an-earlier-run"}
        enrich_with_geo([m], container)
        assert m.extra["geo"] == {"country": "pinned-by-an-earlier-run"}
        assert lookup.calls == []  # idempotent: no lookup happened at all

    def test_failed_measurement_still_enriches_its_ip(self) -> None:
        # A failed probe still learned the target's IP (the DNS
        # resolver answered with NXDOMAIN-no; the http probe resolved
        # then timed out). Geography is about the ADDRESS, not the
        # probe outcome -- the place is attached either way.
        container = Container(
            probe_registry=ProbeRegistry(),
            geo_lookup=FakeGeoLookup(answers={LONDON_IP: GeoInfo(ip_address=LONDON_IP, place=Place(country="United Kingdom"))}),
            ip_resolver=TargetIpResolver(),
        )
        m = RawMeasurement(probe_type=ProbeType.DNS, target="example.com", success=False, error="timed out", extra={"answers": [LONDON_IP]})
        enrich_with_geo([m], container)
        assert m.extra["geo"]["country"] == "United Kingdom"

    def test_ip_literal_target_used_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A target that IS an IP must not reach the resolver at all --
        # the getaddrinfo monkeypatch would explode if it did.
        def boom(host: str, port: int) -> list:
            raise AssertionError("resolver must not be consulted for an IP literal")

        monkeypatch.setattr("netscope.adapters.geoip.socket.getaddrinfo", boom)
        lookup = FakeGeoLookup(answers={LONDON_IP: GeoInfo(ip_address=LONDON_IP, place=Place(country="United Kingdom"))})
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=lookup, ip_resolver=TargetIpResolver())
        m = RawMeasurement(probe_type=ProbeType.ICMP, target=LONDON_IP, success=True, latency_ms=5.0)
        enrich_with_geo([m], container)
        assert m.extra["geo"]["country"] == "United Kingdom"
        assert lookup.calls == [LONDON_IP]

    def test_getaddrinfo_fallback_for_hostnames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "netscope.adapters.geoip.socket.getaddrinfo",
            lambda host, port: [(2, 1, 6, "", (LONDON_IP, 0))],
        )
        lookup = FakeGeoLookup(answers={LONDON_IP: GeoInfo(ip_address=LONDON_IP, place=Place(country="United Kingdom"))})
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=lookup, ip_resolver=TargetIpResolver())
        m = RawMeasurement(probe_type=ProbeType.ICMP, target="example.com", success=True, latency_ms=5.0)
        enrich_with_geo([m], container)
        assert m.extra["geo"]["country"] == "United Kingdom"

    def test_multiple_measurements_each_get_their_own(self) -> None:
        tehran_ip = "5.35.90.77"
        lookup = FakeGeoLookup(
            answers={
                LONDON_IP: GeoInfo(ip_address=LONDON_IP, place=Place(city="London", country="United Kingdom"), distance_km=4396.57),
                tehran_ip: GeoInfo(ip_address=tehran_ip, place=TEHRAN, distance_km=0.0),
            }
        )
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=lookup, ip_resolver=TargetIpResolver())
        first = _measurement("example.com")
        second = RawMeasurement(probe_type=ProbeType.DNS, target="example.net", success=True, latency_ms=9.0, extra={"answers": [tehran_ip]})
        enrich_with_geo([first, second], container)
        assert first.extra["geo"]["distance_km"] == pytest.approx(4396.57)
        assert second.extra["geo"]["distance_km"] == pytest.approx(0.0)


class TestUseCaseWiring:
    def _round_container(self, lookup: FakeGeoLookup) -> Container:
        # Every probe the round touches gets a fake whose scripted
        # extra carries a resolved IP, standing in for the way real
        # probes record what they learned. No gateway (config.gateway
        # is None), so the round is dns-target ICMP + DNS + HTTP.
        probes = {kind: _RoundFakeProbe(kind, extra={"answers": [LONDON_IP]}) for kind in (ProbeType.ICMP, ProbeType.DNS, ProbeType.HTTP)}
        return Container(probe_registry=ProbeRegistry(probes=probes), geo_lookup=lookup, ip_resolver=TargetIpResolver())

    def test_measurement_round_enriches(self) -> None:
        from netscope.core.baseline import UserBaseline

        lookup = FakeGeoLookup(answers={LONDON_IP: GeoInfo(ip_address=LONDON_IP, place=Place(country="United Kingdom"))})
        measurements, _, _ = run_measurement_round(self._round_container(lookup), NetScopeConfig(), baseline=UserBaseline())
        geo_blocks = [m.extra.get("geo") for m in measurements if "geo" in m.extra]
        assert geo_blocks and all(g["country"] == "United Kingdom" for g in geo_blocks)

    def test_check_target_enriches(self) -> None:
        lookup = FakeGeoLookup(answers={LONDON_IP: GeoInfo(ip_address=LONDON_IP, place=Place(country="United Kingdom"))})
        probes = {kind: _RoundFakeProbe(kind, extra={"answers": [LONDON_IP]}) for kind in (ProbeType.ICMP,)}
        container = Container(probe_registry=ProbeRegistry(probes=probes), geo_lookup=lookup, ip_resolver=TargetIpResolver())
        measurements, _, _ = check_target("example.com", container, {ProbeType.ICMP})
        assert measurements and "geo" in measurements[0].extra


@dataclass
class _RoundFakeProbe:
    """Probe fake whose scripted extra carries a resolved IP, standing
    in for the way real probes record what they learned."""

    probe_type: ProbeType
    extra: dict = field(default_factory=dict)

    def run(self, target: str, **options) -> RawMeasurement:
        return RawMeasurement(probe_type=self.probe_type, target=target, success=True, latency_ms=8.0, extra=dict(self.extra))


class TestRouteHopCountry:
    def test_hops_get_country_when_geo_wired(self) -> None:
        from netscope.app.use_cases import _enrich_snapshot

        lookup = FakeGeoLookup(
            answers={
                "81.2.69.1": GeoInfo(ip_address="81.2.69.1", place=Place(country="United Kingdom")),
                "8.8.8.8": GeoInfo(ip_address="8.8.8.8", place=Place(country="United States")),
            }
        )
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=lookup, ip_resolver=TargetIpResolver())
        snapshot = RouteSnapshot(
            target="example.com",
            hops=[
                RouteHop(ttl=1, address="81.2.69.1", hostname=None, avg_rtt_ms=1.0, packet_loss_pct=0.0),
                RouteHop(ttl=2, address="8.8.8.8", hostname=None, avg_rtt_ms=2.0, packet_loss_pct=0.0),
                RouteHop(ttl=3, address=None, hostname=None, avg_rtt_ms=None, packet_loss_pct=100.0),
            ],
        )
        _enrich_snapshot(snapshot, container, resolve_hostnames=False)
        assert snapshot.hops[0].country == "United Kingdom"
        assert snapshot.hops[1].country == "United States"
        assert snapshot.hops[2].country is None  # timeout hop: no address, no geography

    def test_hop_country_fill_is_idempotent(self) -> None:
        from netscope.app.use_cases import _enrich_snapshot

        lookup = FakeGeoLookup(answers={})
        container = Container(probe_registry=ProbeRegistry(), geo_lookup=lookup, ip_resolver=TargetIpResolver())
        hop = RouteHop(ttl=1, address="81.2.69.1", hostname=None, avg_rtt_ms=1.0, packet_loss_pct=0.0, country="Pinned")
        _enrich_snapshot(RouteSnapshot(target="t", hops=[hop]), container, resolve_hostnames=False)
        assert hop.country == "Pinned"
        assert lookup.calls == []


class TestContainerWiring:
    def _fixture_db(self, tmp_path: Path, *, build_epoch: int | None = None) -> Path:
        path = tmp_path / "GeoLite2-City.mmdb"
        if build_epoch is None:
            import time

            build_epoch = int(time.time()) - 2 * 86400
        path.write_bytes(
            build_mmdb(
                {
                    "81.2.69.0/24": {
                        "city": {"names": {"en": "London"}},
                        "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
                        "location": {"latitude": 51.5074, "longitude": -0.1278},
                    },
                    "5.35.90.0/24": {
                        "city": {"names": {"en": "Tehran"}},
                        "country": {"iso_code": "IR", "names": {"en": "Iran"}},
                        "location": {"latitude": 35.6892, "longitude": 51.389},
                    },
                },
                build_epoch=build_epoch,
            )
        )
        return path

    def test_missing_database_wires_nothing(self, tmp_path: Path) -> None:
        assert _open_geo_lookup(NetScopeConfig(geo_db_path=str(tmp_path / "absent.mmdb"))) is None

    def test_corrupt_database_degrades_to_none(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.mmdb"
        path.write_bytes(b"\x00" * 64)
        assert _open_geo_lookup(NetScopeConfig(geo_db_path=str(path))) is None

    def test_home_from_explicit_coordinates(self, tmp_path: Path) -> None:
        lookup = _open_geo_lookup(NetScopeConfig(geo_db_path=str(self._fixture_db(tmp_path)), home_latitude=35.6892, home_longitude=51.389))
        assert lookup is not None
        info = lookup.lookup(LONDON_IP)
        assert info is not None
        assert info.distance_km == pytest.approx(4396.57, abs=0.5)

    def test_home_from_home_ip_lookup(self, tmp_path: Path) -> None:
        lookup = _open_geo_lookup(NetScopeConfig(geo_db_path=str(self._fixture_db(tmp_path)), home_ip="5.35.90.9"))
        assert lookup is not None
        info = lookup.lookup(LONDON_IP)
        assert info is not None
        assert info.distance_km == pytest.approx(4396.57, abs=0.5)

    def test_unresolvable_home_ip_still_wires_place_only(self, tmp_path: Path) -> None:
        lookup = _open_geo_lookup(NetScopeConfig(geo_db_path=str(self._fixture_db(tmp_path)), home_ip="203.0.113.9"))
        assert lookup is not None
        info = lookup.lookup(LONDON_IP)
        assert info is not None
        assert info.place is not None
        assert info.distance_km is None

    def test_build_container_passes_config_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = self._fixture_db(tmp_path)
        # Keep the composition root from touching the real home
        # directory: repositories are stubbed to None via the flags.
        container = build_container(
            with_baseline_persistence=False,
            with_geo_enrichment=True,
            config=NetScopeConfig(geo_db_path=str(db)),
        )
        assert container.geo_lookup is not None
        assert isinstance(container.geo_lookup, GeoLookup)

    def test_build_container_without_geo_flag(self, tmp_path: Path) -> None:
        container = build_container(
            with_baseline_persistence=False,
            with_geo_enrichment=False,
            config=NetScopeConfig(geo_db_path=str(self._fixture_db(tmp_path))),
        )
        assert container.geo_lookup is None

    def test_wired_adapter_discloses_db_age(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from netscope.core.models import utcnow

        now = utcnow()
        monkeypatch.setattr("netscope.adapters.geoip.utcnow", lambda: now)
        lookup = _open_geo_lookup(
            NetScopeConfig(geo_db_path=str(self._fixture_db(tmp_path, build_epoch=int(now.timestamp()) - (DB_STALE_AFTER_DAYS + 5) * 86400)))
        )
        assert lookup is not None
        info = lookup.lookup(LONDON_IP)
        assert info is not None
        assert info.db_age_days == DB_STALE_AFTER_DAYS + 5
        assert info.stale_db is True


class TestRenderGeo:
    """The v0.12 UI surfaces: rich detail, markdown geography, HTML
    geography, JSON serialization -- all fed by the same canonical
    extra["geo"] block."""

    GEO = {
        "ip": LONDON_IP,
        "country": "United Kingdom",
        "country_code": "GB",
        "city": "London",
        "latitude": 51.5074,
        "longitude": -0.1278,
        "distance_km": 4396.57,
        "db": "GeoLite2-City",
        "db_age_days": 2,
        "stale_db": False,
    }

    def test_rich_detail_shows_place_and_distance(self) -> None:
        from netscope.ui.render import _success_detail

        m = _measurement()
        m.extra["geo"] = dict(self.GEO)
        detail = _success_detail(m)
        assert "London, United Kingdom" in detail
        assert "4,397 km from home" in detail
        assert "db 2d old" in detail

    def test_rich_detail_flags_stale_database(self) -> None:
        from netscope.ui.render import _success_detail

        geo = dict(self.GEO, db_age_days=DB_STALE_AFTER_DAYS + 10, stale_db=True)
        m = _measurement()
        m.extra["geo"] = geo
        assert "refresh recommended" in _success_detail(m)

    def test_rich_detail_without_geo_is_unchanged(self) -> None:
        from netscope.ui.render import _success_detail

        m = _measurement()
        assert "geo" not in _success_detail(m)

    def test_report_row_and_target_aggregate_carry_geo(self) -> None:
        from netscope.app.use_cases import _report_row, _target_reports

        first = _measurement("example.com")
        first.extra["geo"] = dict(self.GEO, distance_km=1.0)
        latest = _measurement("example.com")
        latest.extra["geo"] = dict(self.GEO)
        row = _report_row(first)
        assert row.geo is not None and row.geo["distance_km"] == 1.0
        reports = _target_reports([first, latest])
        assert reports[0].geo is not None
        assert reports[0].geo["distance_km"] == pytest.approx(4396.57)  # the NEWEST one wins

    def test_json_report_includes_geo(self) -> None:
        from netscope.app.reports import serialize_evidence_report
        from netscope.app.use_cases import _report_row

        m = _measurement()
        m.extra["geo"] = dict(self.GEO)
        row = _report_row(m)
        assert row.geo is not None and row.geo["country"] == "United Kingdom"
        serialized = serialize_evidence_report(_build_report([m]))
        assert serialized["targets"][0]["geo"]["country"] == "United Kingdom"

    def test_markdown_geography_section(self) -> None:
        from netscope.app.reports import evidence_report_markdown

        m = _measurement()
        m.extra["geo"] = dict(self.GEO)
        report = _build_report([m])
        text = evidence_report_markdown(report)
        assert "## Geography" in text
        assert "`example.com` → London, United Kingdom · 4,397 km from home · db 2d old" in text

    def test_markdown_omits_geography_without_geo(self) -> None:
        from netscope.app.reports import evidence_report_markdown

        assert "## Geography" not in evidence_report_markdown(_build_report([_measurement()]))

    def test_html_geography_section(self) -> None:
        from netscope.app.reports import evidence_report_html

        m = _measurement()
        m.extra["geo"] = dict(self.GEO)
        html = evidence_report_html(_build_report([m]))
        assert "<h2>Geography</h2>" in html
        assert "London, United Kingdom" in html
        assert "4,397 km from home" in html

    def test_html_stale_geo_is_flagged(self) -> None:
        from netscope.app.reports import evidence_report_html

        m = _measurement()
        m.extra["geo"] = dict(self.GEO, db_age_days=DB_STALE_AFTER_DAYS + 1, stale_db=True)
        html = evidence_report_html(_build_report([m]))
        assert "refresh recommended" in html
        assert "class='warn'" in html

    def test_html_omits_geography_without_geo(self) -> None:
        from netscope.app.reports import evidence_report_html

        assert "<h2>Geography</h2>" not in evidence_report_html(_build_report([_measurement()]))

    def test_route_table_has_country_column(self) -> None:
        from netscope.ui.render import route_table

        hop = RouteHop(ttl=1, address="81.2.69.1", hostname=None, avg_rtt_ms=1.0, packet_loss_pct=0.0, country="United Kingdom")
        table = route_table([hop])
        headers = [c.header for c in table.columns]
        assert "Country" in headers


def _build_report(measurements):
    """The minimal EvidenceReport the geography render tests need --
    built through the real grouping/aggregate path, not by hand."""
    from netscope.app.use_cases import EvidenceReport, _target_reports
    from netscope.core.models import utcnow

    return EvidenceReport(
        since=measurements[0].timestamp,
        until=utcnow(),
        rounds=[],
        target_reports=_target_reports(measurements),
        failure_rows=[],
    )
