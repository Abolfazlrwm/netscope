"""
Tests for BaselineRepository (v0.2) -- the SQLite-backed persistence
that makes the personal baseline outlive a process. Focus: exact
Welford round-trips, empty-database semantics, and upsert/mirror
behavior.
"""

from __future__ import annotations

import pytest

from netscope.core.baseline import MetricBaseline, UserBaseline
from netscope.persistence.baseline_repository import BaselineRepository


@pytest.fixture()
def repo(tmp_path):
    repository = BaselineRepository.open(tmp_path / "test.db")
    yield repository
    repository.close()


def test_load_from_empty_database_returns_fresh_baseline(repo):
    baseline = repo.load()
    assert isinstance(baseline, UserBaseline)
    assert baseline.latency == {}
    assert baseline.packet_loss == {}


def test_save_then_load_round_trips_welford_state_exactly(repo):
    baseline = UserBaseline()
    for v in [10.0, 12.0, 11.5, 9.8, 10.9, 13.2]:
        baseline.observe_latency("1.1.1.1", v)
    for v in [0.0, 0.0, 1.0]:
        baseline.observe_packet_loss("1.1.1.1", v)

    repo.save(baseline)
    loaded = repo.load()

    original = baseline.latency["1.1.1.1"]
    restored = loaded.latency["1.1.1.1"]
    assert restored.count == original.count
    assert restored.mean == pytest.approx(original.mean)
    assert restored._m2 == pytest.approx(original._m2)
    assert restored.stddev == pytest.approx(original.stddev)
    assert loaded.packet_loss["1.1.1.1"].count == 3


def test_load_preserves_metric_baseline_semantics(repo):
    """A restored baseline must still detect anomalies the same way the
    in-memory one would -- that's the entire point of persistence."""
    baseline = UserBaseline()
    for v in [10.0, 10.1, 9.9, 10.2, 10.0, 10.1]:
        baseline.observe_latency("gateway", v)
    repo.save(baseline)
    loaded = repo.load().latency["gateway"]

    # The observed mean is ~10.05; the same value scores ~0 sigma, and
    # the anomaly semantics match the in-memory baseline exactly.
    assert loaded.deviation_sigma(10.05) == pytest.approx(0.0, abs=1e-9)
    assert loaded.is_anomalous(50.0) is True
    assert loaded.is_anomalous(10.2) is False


def test_save_mirrors_the_in_memory_baseline_exactly(repo):
    baseline = UserBaseline()
    baseline.observe_latency("a", 5.0)
    baseline.observe_latency("b", 7.0)
    repo.save(baseline)

    # Remove a target in memory and save again: the database must mirror
    # the new state, not accumulate history.
    del baseline.latency["a"]
    repo.save(baseline)

    loaded = repo.load()
    assert set(loaded.latency.keys()) == {"b"}


def test_multiple_targets_and_metric_types_round_trip(repo):
    baseline = UserBaseline()
    baseline.observe_latency("1.1.1.1", 20.0)
    baseline.observe_latency("gateway.local", 2.0)
    baseline.observe_packet_loss("1.1.1.1", 0.5)
    repo.save(baseline)

    loaded = repo.load()
    assert set(loaded.latency) == {"1.1.1.1", "gateway.local"}
    assert set(loaded.packet_loss) == {"1.1.1.1"}


def test_repositories_share_one_schema(tmp_path):
    """BaselineRepository and MeasurementRepository open the same
    database file layout (one ~/.netscope/netscope.db) without schema
    conflicts."""
    from netscope.core.models import ProbeType, RawMeasurement
    from netscope.persistence.measurement_repository import MeasurementRepository

    db = tmp_path / "shared.db"
    baseline_repo = BaselineRepository.open(db)
    measurement_repo = MeasurementRepository.open(db)

    baseline = UserBaseline()
    baseline.observe_latency("t", 1.0)
    baseline_repo.save(baseline)
    measurement_repo.save(RawMeasurement(probe_type=ProbeType.ICMP, target="t", success=True, latency_ms=1.0))

    assert baseline_repo.load().latency["t"].count == 1
    assert len(measurement_repo.recent("t")) == 1
    baseline_repo.close()
    measurement_repo.close()


def test_metric_baseline_direct_construction_still_works():
    """Guard: the dataclass's public surface is unchanged by the
    repository's use of the private _m2 field."""
    metric = MetricBaseline()
    metric.update(4.0)
    metric.update(6.0)
    assert metric.mean == 5.0
    assert metric.stddev == pytest.approx(1.4142, abs=1e-3)


# ---------------------------------------------------------------------------
# Throughput baselines (v0.4)
# ---------------------------------------------------------------------------


def test_throughput_baseline_round_trips_exactly(tmp_path):
    """Welford-exact like latency: count/mean/m2 survive save+load with
    no drift, and the latency rows next to them are untouched."""
    repository = BaselineRepository.open(tmp_path / "tput.db")
    baseline = UserBaseline()
    for v in [88.0, 92.0, 95.0]:
        baseline.observe_throughput("https://speed.example/down", v)
    baseline.observe_latency("gw", 5.0)

    repository.save(baseline)
    loaded = repository.load()

    learned = loaded.throughput["https://speed.example/down"]
    expected = baseline.throughput["https://speed.example/down"]
    assert (learned.count, learned.mean, learned._m2) == (expected.count, expected.mean, expected._m2)
    assert loaded.latency["gw"].count == 1


def test_saving_an_empty_throughput_store_clears_old_rows(tmp_path):
    """save() mirrors the in-memory object exactly -- a cleared store
    must not leave orphaned rows behind."""
    repository = BaselineRepository.open(tmp_path / "tput-clear.db")
    baseline = UserBaseline()
    baseline.observe_throughput("t", 50.0)
    repository.save(baseline)

    repository.save(UserBaseline())

    assert repository.load().throughput == {}


def test_jitter_baseline_round_trips_exactly(tmp_path):
    """v0.13, Welford-exact like its latency/loss/throughput siblings:
    count/mean/m2 survive save+load with no drift, and the stores next
    to it are untouched."""
    repository = BaselineRepository.open(tmp_path / "jitter.db")
    baseline = UserBaseline()
    for v in [2.0, 2.2, 1.8, 2.1, 2.0]:
        baseline.observe_jitter("gw", v)
    baseline.observe_latency("gw", 5.0)

    repository.save(baseline)
    loaded = repository.load()

    learned = loaded.jitter["gw"]
    expected = baseline.jitter["gw"]
    assert (learned.count, learned.mean, learned._m2) == (expected.count, expected.mean, expected._m2)
    assert loaded.latency["gw"].count == 1


def test_saving_an_empty_jitter_store_clears_old_rows(tmp_path):
    """save() mirrors the in-memory object exactly -- a cleared jitter
    store must not leave orphaned rows behind."""
    repository = BaselineRepository.open(tmp_path / "jitter-clear.db")
    baseline = UserBaseline()
    baseline.observe_jitter("t", 2.0)
    repository.save(baseline)

    repository.save(UserBaseline())

    assert repository.load().jitter == {}
