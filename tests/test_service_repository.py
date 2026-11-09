"""
Tests for netscope.persistence.service_repository.

Round-trips the curated service list against a real (temp-file) SQLite
database -- the same repository-under-SQLite pattern
test_measurement_repository.py and test_baseline_repository.py
established. Everything is offline: no probe runs, no network, no
~/.netscope writes (every test opens its own tmp_path database).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from netscope.core.models import ProbeType, Service
from netscope.persistence.service_repository import ServiceRepository


@pytest.fixture()
def repo(tmp_path: Path) -> ServiceRepository:
    return ServiceRepository.open(tmp_path / "services.db")


def _vpn_service(**overrides: object) -> Service:
    fields: dict[str, object] = {
        "name": "My VPN",
        "host": "vpn.example.net",
        "enabled_checks": {ProbeType.ICMP, ProbeType.HTTP},
    }
    fields.update(overrides)
    return Service(**fields)  # type: ignore[arg-type]


def test_open_creates_and_initializes_the_database_file(tmp_path: Path):
    db = tmp_path / "nested" / "services.db"
    assert not db.exists()
    ServiceRepository.open(db)
    assert db.exists()


def test_round_trips_a_service_exactly(repo: ServiceRepository):
    repo.save(_vpn_service())

    loaded = repo.load_all()
    assert len(loaded) == 1
    assert loaded[0].name == "My VPN"
    assert loaded[0].host == "vpn.example.net"
    assert loaded[0].enabled_checks == {ProbeType.ICMP, ProbeType.HTTP}


def test_round_trips_all_five_checks(repo: ServiceRepository):
    all_five = {ProbeType.ICMP, ProbeType.DNS, ProbeType.TCP, ProbeType.TLS, ProbeType.HTTP}
    repo.save(_vpn_service(enabled_checks=all_five))

    loaded = repo.load_all()
    assert loaded[0].enabled_checks == all_five


def test_round_trips_an_empty_check_set(repo: ServiceRepository):
    """A Service with no checks enabled yet is valid ("each optional")
    and must survive the round trip as no checks, not as a failure."""
    repo.save(_vpn_service(enabled_checks=set()))

    loaded = repo.load_all()
    assert loaded[0].enabled_checks == set()


def test_checks_are_stored_sorted_and_comma_separated(repo: ServiceRepository):
    """The storage contract: byte-identical rows for the same set
    regardless of Python's set iteration order (see the module
    docstring)."""
    repo.save(_vpn_service(enabled_checks={ProbeType.HTTP, ProbeType.ICMP, ProbeType.DNS}))
    repo.save(_vpn_service(enabled_checks={ProbeType.DNS, ProbeType.HTTP, ProbeType.ICMP}))

    stored = repo._conn.execute("SELECT checks FROM services WHERE name = 'My VPN'").fetchone()[0]
    assert stored == "dns,http,icmp"


def test_upsert_by_name_replaces_in_place(repo: ServiceRepository):
    repo.save(_vpn_service())
    repo.save(_vpn_service(host="new.example.net", enabled_checks={ProbeType.TLS}))

    loaded = repo.load_all()
    assert len(loaded) == 1
    assert loaded[0].host == "new.example.net"
    assert loaded[0].enabled_checks == {ProbeType.TLS}


def test_upsert_keeps_rowid_so_an_unchanged_save_rewrites_nothing_meaningful(repo: ServiceRepository):
    """ON CONFLICT DO UPDATE updates in place -- unlike INSERT OR
    REPLACE it does not delete-then-insert the row (see the module
    docstring)."""
    repo.save(_vpn_service())
    rowid_before = repo._conn.execute("SELECT rowid FROM services").fetchone()[0]

    repo.save(_vpn_service())  # same data, still an upsert

    rowid_after = repo._conn.execute("SELECT rowid FROM services").fetchone()[0]
    assert rowid_after == rowid_before


def test_load_all_orders_alphabetically_by_name(repo: ServiceRepository):
    repo.save(_vpn_service(name="zebra panel", host="z.example.net"))
    repo.save(_vpn_service(name="alpha cdn", host="a.example.net"))
    repo.save(_vpn_service(name="middle mail", host="m.example.net"))

    assert [svc.name for svc in repo.load_all()] == ["alpha cdn", "middle mail", "zebra panel"]


def test_get_returns_one_service_or_none(repo: ServiceRepository):
    repo.save(_vpn_service())

    found = repo.get("My VPN")
    assert found is not None
    assert found.host == "vpn.example.net"
    assert repo.get("never saved") is None


def test_get_is_exact_name_match(repo: ServiceRepository):
    repo.save(_vpn_service(name="My VPN"))

    assert repo.get("my vpn") is None  # curated names are case-sensitive identity
    assert repo.get("My V") is None


def test_remove_reports_whether_a_row_existed(repo: ServiceRepository):
    repo.save(_vpn_service())

    assert repo.remove("My VPN") is True
    assert repo.load_all() == []
    assert repo.remove("My VPN") is False
    assert repo.remove("never saved") is False


def test_load_all_on_an_empty_curated_list_returns_an_empty_list(repo: ServiceRepository):
    assert repo.load_all() == []


def test_a_corrupted_checks_value_fails_loudly_not_silently(repo: ServiceRepository):
    """A row with an unknown check value raises from the enum
    constructor rather than being skipped -- quietly dropping one of
    the user's saved services would be worse than failing loudly (see
    the module docstring)."""
    repo.save(_vpn_service())
    repo._conn.execute("UPDATE services SET checks = 'icmp,bogus' WHERE name = 'My VPN'")
    repo._conn.commit()

    with pytest.raises(ValueError):
        repo.load_all()


def test_no_sqlite3_row_crosses_the_repository_boundary(repo: ServiceRepository):
    repo.save(_vpn_service())

    for svc in repo.load_all():
        assert isinstance(svc, Service)
    assert isinstance(repo.get("My VPN"), Service)


def test_two_repositories_on_one_file_share_the_list(tmp_path: Path):
    db = tmp_path / "shared.db"
    writer = ServiceRepository.open(db)
    writer.save(_vpn_service())

    reader = ServiceRepository.open(db)
    loaded = reader.load_all()
    assert len(loaded) == 1
    assert loaded[0].host == "vpn.example.net"


def test_repository_survives_a_reopen(tmp_path: Path):
    """The curated list outlives the process that saved it -- the whole
    point of the storage half."""
    db = tmp_path / "persistent.db"
    ServiceRepository.open(db).save(_vpn_service())

    reopened = ServiceRepository.open(db)
    loaded = reopened.load_all()
    assert len(loaded) == 1
    assert loaded[0].name == "My VPN"


def test_existing_measurements_are_untouched_by_service_writes(tmp_path: Path):
    """v0.7 is additive: curating services never disturbs the
    measurement log sharing the same database file."""
    db = tmp_path / "mixed.db"
    measurement_repo_conn = sqlite3.connect(db)
    from netscope.persistence.schema import initialize_database

    initialize_database(measurement_repo_conn)
    measurement_repo_conn.execute(
        "INSERT INTO measurements (probe_type, target, timestamp, success) VALUES ('icmp', 'gw', '2026-11-09T08:00:00+00:00', 1)"
    )
    measurement_repo_conn.commit()
    measurement_repo_conn.close()

    repo = ServiceRepository.open(db)
    repo.save(_vpn_service())

    check = sqlite3.connect(db)
    row = check.execute("SELECT target, success FROM measurements").fetchone()
    assert row == ("gw", 1)
    check.close()
