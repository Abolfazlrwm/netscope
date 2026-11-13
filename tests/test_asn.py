"""
tests.test_asn

`netscope.core.asn` -- the v0.14 ownership record. Pure domain policy:
no database, no network, no file I/O, exactly the offline discipline
tests/test_geo.py established for the geography policy it pairs with.
"""

from __future__ import annotations

from netscope.core.asn import AsnInfo, asn_extra
from netscope.core.geo import DB_STALE_AFTER_DAYS


class TestAsnInfoShape:
    def test_minimal_record_is_the_number_only(self) -> None:
        info = AsnInfo(ip_address="1.1.1.1", asn=13335)
        assert info.asn == 13335
        assert info.organization is None
        assert info.prefix is None
        assert info.db_type is None
        assert info.db_age_days is None

    def test_full_record_carries_every_disclosure_field(self) -> None:
        info = AsnInfo(
            ip_address="1.1.1.1",
            asn=13335,
            organization="Cloudflare, Inc.",
            prefix="1.1.1.0/24",
            db_type="GeoLite2-ASN",
            db_age_days=3,
        )
        assert info.organization == "Cloudflare, Inc."
        assert info.prefix == "1.1.1.0/24"
        assert info.db_type == "GeoLite2-ASN"
        assert info.db_age_days == 3

    def test_matched_requires_a_number(self) -> None:
        assert AsnInfo(ip_address="1.1.1.1", asn=13335).matched is True
        assert AsnInfo(ip_address="1.1.1.1", asn=None).matched is False

    def test_organization_none_is_not_matched_fabrication(self) -> None:
        # The TASK-022 text table's honest shape: a number, never a name.
        info = AsnInfo(ip_address="9.9.9.9", asn=19281, organization=None)
        assert info.matched and info.organization is None


class TestStaleDbPolicy:
    def test_unknown_age_is_not_stale(self) -> None:
        # The text table carries no build date -- absence of evidence
        # is not evidence of age.
        assert AsnInfo(ip_address="1.1.1.1", asn=13335, db_age_days=None).stale_db is False

    def test_fresh_database_is_not_stale(self) -> None:
        info = AsnInfo(ip_address="1.1.1.1", asn=13335, db_age_days=DB_STALE_AFTER_DAYS)
        assert info.stale_db is False

    def test_database_past_the_rhythm_is_stale(self) -> None:
        info = AsnInfo(ip_address="1.1.1.1", asn=13335, db_age_days=DB_STALE_AFTER_DAYS + 1)
        assert info.stale_db is True

    def test_policy_is_geography_policy_not_a_second_number(self) -> None:
        # One EULA rhythm for both GeoLite2 editions: the constant is
        # borrowed, never redefined here.
        from netscope.core import asn as asn_module

        assert asn_module.DB_STALE_AFTER_DAYS is DB_STALE_AFTER_DAYS


class TestAsnExtra:
    def test_canonical_key_set(self) -> None:
        payload = asn_extra(
            AsnInfo(
                ip_address="1.1.1.1",
                asn=13335,
                organization="Cloudflare, Inc.",
                prefix="1.1.1.0/24",
                db_type="GeoLite2-ASN",
                db_age_days=3,
            )
        )
        assert payload == {
            "ip": "1.1.1.1",
            "asn": 13335,
            "organization": "Cloudflare, Inc.",
            "db": "GeoLite2-ASN",
            "db_age_days": 3,
            "stale_db": False,
        }

    def test_json_scalars_only(self) -> None:
        # Every value survives json.dumps untouched -- the payload
        # rides extra_json persistence and the _json_safe pass.
        import json

        payload = asn_extra(AsnInfo(ip_address="9.9.9.9", asn=19281, organization="Quad9"))
        assert json.loads(json.dumps(payload)) == payload

    def test_prefix_stays_off_the_payload(self) -> None:
        # The matched prefix is lookup plumbing, not an ownership fact.
        payload = asn_extra(AsnInfo(ip_address="1.1.1.1", asn=13335, prefix="1.1.1.0/24"))
        assert "prefix" not in payload

    def test_unmatched_record_serializes_honestly(self) -> None:
        payload = asn_extra(AsnInfo(ip_address="10.0.0.1", asn=None))
        assert payload["asn"] is None
        assert payload["organization"] is None
