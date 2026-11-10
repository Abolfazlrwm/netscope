"""
Tests for netscope.persistence.route_repository.

Fully offline: every test runs against a temporary SQLite file created
through `schema.create_connection` (the one canonical
schema-initialization path), with hand-constructed RouteSnapshots -- no
traceroute probe, no network access, consistent with the rest of the
persistence test suite (test_measurement_repository.py,
test_baseline_repository.py, test_service_repository.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netscope.core.models import RouteHop, RouteSnapshot
from netscope.persistence.route_repository import RouteRepository
from netscope.persistence.schema import SCHEMA_VERSION, create_connection


@pytest.fixture()
def repo(tmp_path):
    repository = RouteRepository.open(tmp_path / "routes.db")
    yield repository
    repository.close()


def _hop(address: str | None, *, ttl: int = 1, hostname: str | None = None, avg_rtt_ms: float | None = 12.5, packet_loss_pct: float = 0.0, asn: str | None = None, organization: str | None = None, country: str | None = None, is_unstable: bool = False) -> RouteHop:
    return RouteHop(
        ttl=ttl,
        address=address,
        hostname=hostname,
        avg_rtt_ms=avg_rtt_ms,
        packet_loss_pct=packet_loss_pct,
        asn=asn,
        organization=organization,
        country=country,
        is_unstable=is_unstable,
    )


def _snapshot(target: str, addresses: list[str | None], *, timestamp: datetime | None = None) -> RouteSnapshot:
    return RouteSnapshot(
        target=target,
        timestamp=timestamp or datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
        hops=[_hop(addr, ttl=i + 1) for i, addr in enumerate(addresses)],
    )


# ---------------------------------------------------------------------------
# save / load roundtrip
# ---------------------------------------------------------------------------


def test_save_then_load_recent_round_trips_a_snapshot(repo):
    snapshot = _snapshot("example.com", ["10.0.0.1", "10.0.0.2", "198.51.100.7"])

    repo.save(snapshot)
    loaded = repo.load_recent("example.com", limit=10)

    assert len(loaded) == 1
    assert loaded[0].target == "example.com"
    assert loaded[0].timestamp == snapshot.timestamp
    assert [hop.address for hop in loaded[0].hops] == ["10.0.0.1", "10.0.0.2", "198.51.100.7"]


def test_roundtrip_preserves_every_hop_field(repo):
    """The hop row is the evidence -- RTT, loss, enrichment (ASN/org/
    country/hostname), and the unstable flag must all survive storage
    exactly as the traceroute probe + enrichment produced them."""
    snapshot = RouteSnapshot(
        target="example.com",
        timestamp=datetime(2026, 11, 10, 9, 15, tzinfo=UTC),
        hops=[
            _hop("10.0.0.1", ttl=1, hostname="gateway.local", avg_rtt_ms=1.25, packet_loss_pct=0.0, is_unstable=False),
            _hop("10.0.0.2", ttl=2, avg_rtt_ms=None, packet_loss_pct=100.0, is_unstable=True),
            _hop("198.51.100.7", ttl=3, avg_rtt_ms=44.0, packet_loss_pct=5.0, asn="AS64496", organization="Example ISP", country="US"),
        ],
    )

    repo.save(snapshot)
    loaded = repo.load_recent("example.com", limit=10)

    assert loaded[0].hops == snapshot.hops


def test_star_hops_survive_storage(repo):
    """A no-reply hop (address=None) is honest evidence -- '*' in the
    signature -- and must round-trip as None, never coerced to a
    placeholder address."""
    snapshot = _snapshot("example.com", ["10.0.0.1", None, "198.51.100.7"])

    repo.save(snapshot)
    loaded = repo.load_recent("example.com", limit=10)

    assert [hop.address for hop in loaded[0].hops] == ["10.0.0.1", None, "198.51.100.7"]


def test_snapshot_with_no_hops_round_trips(repo):
    repo.save(_snapshot("example.com", []))
    loaded = repo.load_recent("example.com", limit=10)
    assert len(loaded) == 1
    assert loaded[0].hops == []


def test_save_is_append_only_never_overwrites(repo):
    """Two saves of identical paths produce two observations -- a route
    log is append-only evidence, not a curated list."""
    path = ["10.0.0.1", "10.0.0.2"]
    repo.save(_snapshot("example.com", path, timestamp=datetime(2026, 11, 10, 9, 0, tzinfo=UTC)))
    repo.save(_snapshot("example.com", path, timestamp=datetime(2026, 11, 10, 9, 30, tzinfo=UTC)))

    loaded = repo.load_recent("example.com", limit=10)

    assert len(loaded) == 2
    assert [snap.timestamp.minute for snap in loaded] == [0, 30]  # oldest first


# ---------------------------------------------------------------------------
# Ordering / window semantics
# ---------------------------------------------------------------------------


def test_load_recent_returns_oldest_first_chronological(repo):
    """The port contract: `analyze_route_churn` compares consecutive
    pairs without sorting -- oldest-first is what makes its changes
    point forward in time."""
    for minutes in (50, 40, 30, 20, 10):  # saved newest first
        repo.save(_snapshot("example.com", ["10.0.0.1"], timestamp=datetime(2026, 11, 10, 9, minutes, tzinfo=UTC)))

    loaded = repo.load_recent("example.com", limit=10)

    assert [snap.timestamp.minute for snap in loaded] == [10, 20, 30, 40, 50]


def test_load_recent_limit_returns_the_most_recent_window_oldest_first(repo):
    for minutes in (50, 40, 30, 20, 10):
        repo.save(_snapshot("example.com", ["10.0.0.1"], timestamp=datetime(2026, 11, 10, 9, minutes, tzinfo=UTC)))

    loaded = repo.load_recent("example.com", limit=2)

    assert [snap.timestamp.minute for snap in loaded] == [40, 50]  # the two newest (9:40, 9:50), oldest first


def test_load_recent_is_scoped_to_one_target(repo):
    repo.save(_snapshot("example.com", ["10.0.0.1"], timestamp=datetime(2026, 11, 10, 9, 0, tzinfo=UTC)))
    repo.save(_snapshot("other.example.com", ["10.0.0.9"], timestamp=datetime(2026, 11, 10, 9, 10, tzinfo=UTC)))

    loaded = repo.load_recent("example.com", limit=10)

    assert len(loaded) == 1
    assert loaded[0].target == "example.com"


def test_load_recent_with_nothing_saved_returns_empty_list(repo):
    assert repo.load_recent("never-traced.example.com", limit=10) == []


def test_load_recent_rejects_a_non_positive_limit(repo):
    with pytest.raises(ValueError):
        repo.load_recent("example.com", limit=0)
    with pytest.raises(ValueError):
        repo.load_recent("example.com", limit=-3)


def test_timestamp_ties_break_by_insert_id_newest_wins(repo):
    """Two runs in the same microsecond (or a clock that stopped): the
    later INSERT is the newer observation -- `id DESC` tie-break."""
    stamp = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    repo.save(_snapshot("example.com", ["10.0.0.1"], timestamp=stamp))
    repo.save(_snapshot("example.com", ["10.0.0.2"], timestamp=stamp))

    loaded = repo.load_recent("example.com", limit=10)

    assert [snap.hops[0].address for snap in loaded] == ["10.0.0.1", "10.0.0.2"]


# ---------------------------------------------------------------------------
# Schema integration
# ---------------------------------------------------------------------------


def test_open_uses_the_canonical_schema_path_without_a_migration(tmp_path):
    """The route tables date from schema v1 -- opening them needs no
    SCHEMA_VERSION bump, and a fresh file still initializes at the
    current version through create_connection."""
    db_path = tmp_path / "routes.db"
    conn = create_connection(db_path)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.close()

    assert version == SCHEMA_VERSION

    repository = RouteRepository.open(db_path)
    repository.save(_snapshot("example.com", ["10.0.0.1"]))
    assert len(repository.load_recent("example.com", limit=5)) == 1
    repository.close()


def test_saved_history_feeds_analyze_route_churn_across_runs(repo):
    """The integration this whole feature exists for: snapshots saved by
    separate `save()` calls, loaded oldest-first, form exactly the
    chronological sequence the churn analyzer compares."""
    from netscope.core.routing import analyze_route_churn

    repo.save(_snapshot("example.com", ["10.0.0.1", "10.0.0.2"], timestamp=datetime(2026, 11, 10, 9, 0, tzinfo=UTC)))
    repo.save(_snapshot("example.com", ["10.0.0.1", "10.0.0.9"], timestamp=datetime(2026, 11, 10, 9, 30, tzinfo=UTC)))

    history = repo.load_recent("example.com", limit=20)
    result = analyze_route_churn(history)

    assert result.snapshot_count == 2
    assert result.change_count == 1
    assert result.last_change is not None
    assert [(h.ttl, h.previous_address, h.new_address) for h in result.last_change.changed_hops] == [(2, "10.0.0.2", "10.0.0.9")]


def test_time_window_survives_a_save_load_cycle_with_sub_second_precision(repo):
    """ISO-8601 fixed-offset storage keeps microseconds -- two runs 1.5
    seconds apart stay 1.5 seconds apart after a roundtrip."""
    first = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
    second = first + timedelta(seconds=1, microseconds=500000)
    repo.save(_snapshot("example.com", ["10.0.0.1"], timestamp=first))
    repo.save(_snapshot("example.com", ["10.0.0.1"], timestamp=second))

    loaded = repo.load_recent("example.com", limit=10)

    assert (loaded[1].timestamp - loaded[0].timestamp) == timedelta(seconds=1, microseconds=500000)
