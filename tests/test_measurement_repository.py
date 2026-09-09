"""
Tests for netscope.persistence.measurement_repository (TASK-030/031).

Per future-roadmap.md's TASK-030 row: "Unit tests against temp-file
SQLite; verify no sqlite3.Row crosses the repository boundary." TASK-031
extends this with history-query tests: target/time-range/probe-type
filtering, ordering, and limit validation for `history()` (and,
transitively, `recent()`, now a thin wrapper around it).

All tests use `:memory:` databases (via MeasurementRepository.open) --
fully offline, deterministic, nothing left on disk.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement
from netscope.persistence.measurement_repository import MeasurementRepository


def _repo() -> MeasurementRepository:
    return MeasurementRepository.open(":memory:")


def _measurement(**overrides) -> RawMeasurement:
    defaults = dict(
        probe_type=ProbeType.ICMP,
        target="1.1.1.1",
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        success=True,
        latency_ms=12.5,
        packet_loss_pct=0.0,
        jitter_ms=1.2,
    )
    defaults.update(overrides)
    return RawMeasurement(**defaults)


# ---------------------------------------------------------------------------
# Save / load / round-trip
# ---------------------------------------------------------------------------


def test_save_does_not_raise():
    repo = _repo()
    repo.save(_measurement())
    repo.close()


def test_saved_measurement_can_be_loaded_back():
    repo = _repo()
    repo.save(_measurement())
    loaded = repo.recent("1.1.1.1")
    assert len(loaded) == 1
    repo.close()


def test_successful_measurement_round_trips_with_equivalent_values():
    original = _measurement(success=True, latency_ms=8.3, packet_loss_pct=0.0, jitter_ms=0.5)
    repo = _repo()
    repo.save(original)
    (loaded,) = repo.recent(original.target)

    assert loaded.probe_type == original.probe_type
    assert loaded.target == original.target
    assert loaded.timestamp == original.timestamp
    assert loaded.success == original.success
    assert loaded.latency_ms == original.latency_ms
    assert loaded.packet_loss_pct == original.packet_loss_pct
    assert loaded.jitter_ms == original.jitter_ms
    assert loaded.error is None
    assert loaded.error_type is None
    repo.close()


def test_failed_measurement_round_trips_with_error_and_error_type():
    original = _measurement(
        success=False,
        latency_ms=None,
        packet_loss_pct=None,
        jitter_ms=None,
        error="connection timed out",
        error_type=ProbeErrorType.TIMEOUT,
    )
    repo = _repo()
    repo.save(original)
    (loaded,) = repo.recent(original.target)

    assert loaded.success is False
    assert loaded.error == "connection timed out"
    assert loaded.error_type == ProbeErrorType.TIMEOUT
    assert loaded.latency_ms is None
    repo.close()


def test_probe_type_reconstructs_as_the_enum_not_a_raw_string():
    repo = _repo()
    repo.save(_measurement(probe_type=ProbeType.DNS))
    (loaded,) = repo.recent("1.1.1.1")
    assert loaded.probe_type is ProbeType.DNS
    assert isinstance(loaded.probe_type, ProbeType)
    repo.close()


def test_error_type_reconstructs_as_the_enum_when_present():
    repo = _repo()
    repo.save(_measurement(success=False, error_type=ProbeErrorType.DNS_FAILURE))
    (loaded,) = repo.recent("1.1.1.1")
    assert loaded.error_type is ProbeErrorType.DNS_FAILURE
    repo.close()


def test_error_type_is_none_when_measurement_succeeded():
    repo = _repo()
    repo.save(_measurement(success=True))
    (loaded,) = repo.recent("1.1.1.1")
    assert loaded.error_type is None
    repo.close()


def test_timestamp_reconstructs_as_a_timezone_aware_datetime():
    ts = datetime(2026, 3, 14, 15, 9, 26, tzinfo=timezone.utc)
    repo = _repo()
    repo.save(_measurement(timestamp=ts))
    (loaded,) = repo.recent("1.1.1.1")
    assert isinstance(loaded.timestamp, datetime)
    assert loaded.timestamp == ts
    assert loaded.timestamp.tzinfo is not None
    repo.close()


def test_extra_dict_round_trips_through_json():
    repo = _repo()
    repo.save(_measurement(extra={"resolved_ip": "93.184.216.34", "status_code": 200}))
    (loaded,) = repo.recent("1.1.1.1")
    assert loaded.extra == {"resolved_ip": "93.184.216.34", "status_code": 200}
    repo.close()


def test_empty_extra_round_trips_as_empty_dict():
    repo = _repo()
    repo.save(_measurement())
    (loaded,) = repo.recent("1.1.1.1")
    assert loaded.extra == {}
    repo.close()


# ---------------------------------------------------------------------------
# recent() behavior
# ---------------------------------------------------------------------------


def test_recent_orders_newest_first():
    repo = _repo()
    older = _measurement(timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), latency_ms=10.0)
    newer = _measurement(timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc), latency_ms=20.0)
    repo.save(older)
    repo.save(newer)
    loaded = repo.recent("1.1.1.1")
    assert [m.latency_ms for m in loaded] == [20.0, 10.0]
    repo.close()


def test_recent_respects_limit():
    repo = _repo()
    for i in range(5):
        repo.save(_measurement(timestamp=datetime(2026, 1, 1 + i, tzinfo=timezone.utc)))
    loaded = repo.recent("1.1.1.1", limit=2)
    assert len(loaded) == 2
    repo.close()


def test_recent_only_returns_the_requested_target():
    repo = _repo()
    repo.save(_measurement(target="1.1.1.1"))
    repo.save(_measurement(target="8.8.8.8"))
    loaded = repo.recent("1.1.1.1")
    assert len(loaded) == 1
    assert loaded[0].target == "1.1.1.1"
    repo.close()


def test_recent_with_no_saved_measurements_returns_empty_list():
    repo = _repo()
    assert repo.recent("never-seen.example") == []
    repo.close()


# ---------------------------------------------------------------------------
# No sqlite3.Row leakage
# ---------------------------------------------------------------------------


def test_recent_never_returns_sqlite3_row_objects():
    repo = _repo()
    repo.save(_measurement())
    loaded = repo.recent("1.1.1.1")
    assert all(isinstance(m, RawMeasurement) for m in loaded)
    assert all(not isinstance(m, sqlite3.Row) for m in loaded)
    repo.close()


def test_measurement_repository_module_does_not_return_sqlite3_row_from_any_public_method():
    """Static check complementing the runtime check above: no method on
    MeasurementRepository whose name doesn't start with `_` should have
    a return-type annotation mentioning sqlite3.Row."""
    import inspect

    for name, method in inspect.getmembers(MeasurementRepository, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        sig = inspect.signature(method)
        assert "Row" not in str(sig.return_annotation)


# ---------------------------------------------------------------------------
# Reuses TASK-029's schema/connection utilities -- no duplicated DDL
# ---------------------------------------------------------------------------


def test_measurement_repository_delegates_schema_creation_to_persistence_schema():
    """Regression guard against a second, competing schema-init path:
    the module must import create_connection from persistence.schema
    rather than defining its own CREATE TABLE statements."""
    import netscope.persistence.measurement_repository as module

    source = inspect_source = open(module.__file__).read()
    assert "CREATE TABLE" not in source
    assert "from netscope.persistence.schema import create_connection" in source


# ---------------------------------------------------------------------------
# Legacy sqlite_store.py migration
# ---------------------------------------------------------------------------


def test_sqlite_store_module_no_longer_exists():
    """sqlite_store.py is retired -- MeasurementRepository is its
    successor (future-roadmap.md TASK-030), not a second, competing
    persistence API alongside it."""
    import importlib

    try:
        importlib.import_module("netscope.persistence.sqlite_store")
        assert False, "sqlite_store module should have been removed"
    except ModuleNotFoundError:
        pass


def test_no_production_code_still_imports_sqlite_store():
    """Checks actual import statements only -- docstrings/comments that
    reference the retired module by name (e.g. explaining the TASK-030
    migration) are legitimate and not a stale-import regression."""
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[1] / "src"
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "sqlite_store" not in alias.name, f"stale import in {path}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert "sqlite_store" not in node.module, f"stale import in {path}"


# ---------------------------------------------------------------------------
# Core dependency isolation
# ---------------------------------------------------------------------------


def test_core_modules_do_not_import_sqlite3():
    import netscope.core.diagnosis
    import netscope.core.incidents
    import netscope.core.models
    import netscope.core.scoring
    import netscope.core.baseline

    for module in (
        netscope.core.diagnosis,
        netscope.core.incidents,
        netscope.core.models,
        netscope.core.scoring,
        netscope.core.baseline,
    ):
        tree = ast.parse(open(module.__file__).read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "sqlite3" not in imported


def test_measurement_repository_does_not_import_ui_or_adapters():
    import netscope.persistence.measurement_repository as module

    tree = ast.parse(open(module.__file__).read())
    full_paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            full_paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            full_paths.add(node.module)

    for forbidden in ("netscope.ui", "netscope.adapters"):
        assert forbidden not in full_paths


# ---------------------------------------------------------------------------
# Transaction behavior
# ---------------------------------------------------------------------------


def test_save_is_wrapped_in_a_transaction():
    """A single INSERT is already atomic in SQLite, but this confirms
    the implementation makes that explicit via `with self._conn:`
    rather than relying on incidental single-statement behavior."""
    import inspect

    source = inspect.getsource(MeasurementRepository.save)
    assert "with self._conn" in source


# ===========================================================================
# TASK-031 -- history() query tests
# ===========================================================================


def _day(i: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, i, hour, tzinfo=timezone.utc)


def _m(day: int, target="1.1.1.1", probe_type=ProbeType.ICMP, success=True) -> RawMeasurement:
    return RawMeasurement(probe_type=probe_type, target=target, timestamp=_day(day), success=success)


# --- A. Empty history ------------------------------------------------------


def test_history_for_a_target_with_no_measurements_returns_empty_list():
    repo = _repo()
    assert repo.history("never-seen.example") == []
    repo.close()


# --- B. Target filtering -----------------------------------------------------


def test_history_never_returns_another_targets_measurements():
    repo = _repo()
    repo.save(_m(1, target="1.1.1.1"))
    repo.save(_m(1, target="8.8.8.8"))
    results = repo.history("1.1.1.1")
    assert len(results) == 1
    assert all(r.target == "1.1.1.1" for r in results)
    repo.close()


# --- C. Full history (no time range) -----------------------------------------


def test_history_without_a_time_range_returns_all_matching_measurements():
    repo = _repo()
    for day in [1, 2, 3, 4, 5]:
        repo.save(_m(day))
    assert len(repo.history("1.1.1.1")) == 5
    repo.close()


# --- D. Start boundary ---------------------------------------------------------


def test_history_start_only_includes_the_boundary_and_everything_after():
    repo = _repo()
    for day in [1, 2, 3]:
        repo.save(_m(day))
    results = repo.history("1.1.1.1", start=_day(2))
    assert sorted(r.timestamp.day for r in results) == [2, 3]  # day 2 (boundary) included, day 1 excluded
    repo.close()


def test_history_start_excludes_measurements_strictly_before_it():
    repo = _repo()
    repo.save(_m(1))
    results = repo.history("1.1.1.1", start=_day(2))
    assert results == []
    repo.close()


# --- E. End boundary -------------------------------------------------------------


def test_history_end_only_includes_the_boundary_and_everything_before():
    repo = _repo()
    for day in [1, 2, 3]:
        repo.save(_m(day))
    results = repo.history("1.1.1.1", end=_day(2))
    assert sorted(r.timestamp.day for r in results) == [1, 2]  # day 2 (boundary) included, day 3 excluded
    repo.close()


def test_history_end_excludes_measurements_strictly_after_it():
    repo = _repo()
    repo.save(_m(3))
    results = repo.history("1.1.1.1", end=_day(2))
    assert results == []
    repo.close()


# --- F. Start + end range, exact boundary matrix --------------------------------


def test_history_start_and_end_range_includes_exactly_the_boundary_and_inside_values():
    repo = _repo()
    repo.save(_m(1))  # before range
    repo.save(_m(2))  # at start boundary
    repo.save(_m(3))  # inside range
    repo.save(_m(4))  # at end boundary
    repo.save(_m(5))  # after range

    results = repo.history("1.1.1.1", start=_day(2), end=_day(4))
    assert sorted(r.timestamp.day for r in results) == [2, 3, 4]
    repo.close()


# --- G. ProbeType filtering ------------------------------------------------------


def test_history_probe_type_filter_returns_only_the_requested_type():
    repo = _repo()
    repo.save(_m(1, probe_type=ProbeType.ICMP))
    repo.save(_m(2, probe_type=ProbeType.DNS))
    repo.save(_m(3, probe_type=ProbeType.HTTP))

    results = repo.history("1.1.1.1", probe_type=ProbeType.DNS)
    assert len(results) == 1
    assert results[0].probe_type == ProbeType.DNS
    repo.close()


# --- H. No ProbeType filter --------------------------------------------------------


def test_history_without_a_probe_type_filter_returns_every_type():
    repo = _repo()
    repo.save(_m(1, probe_type=ProbeType.ICMP))
    repo.save(_m(2, probe_type=ProbeType.DNS))
    results = repo.history("1.1.1.1")
    assert {r.probe_type for r in results} == {ProbeType.ICMP, ProbeType.DNS}
    repo.close()


# --- I. Ordering ------------------------------------------------------------------


def test_history_orders_by_timestamp_not_insertion_order():
    repo = _repo()
    for day in [3, 1, 5, 2, 4]:  # deliberately out of order
        repo.save(_m(day))
    results = repo.history("1.1.1.1")
    assert [r.timestamp.day for r in results] == [5, 4, 3, 2, 1]  # newest first, documented default
    repo.close()


# --- J. Limit ---------------------------------------------------------------------


def test_history_limit_smaller_than_available_results():
    repo = _repo()
    for day in [1, 2, 3]:
        repo.save(_m(day))
    assert len(repo.history("1.1.1.1", limit=2)) == 2


def test_history_limit_equal_to_available_results():
    repo = _repo()
    for day in [1, 2, 3]:
        repo.save(_m(day))
    assert len(repo.history("1.1.1.1", limit=3)) == 3


def test_history_limit_larger_than_available_results():
    repo = _repo()
    for day in [1, 2, 3]:
        repo.save(_m(day))
    assert len(repo.history("1.1.1.1", limit=100)) == 3


def test_history_limit_none_means_unlimited():
    repo = _repo()
    for day in range(1, 11):
        repo.save(_m(day))
    assert len(repo.history("1.1.1.1", limit=None)) == 10


# --- K. Invalid limits ------------------------------------------------------------


def test_history_limit_zero_raises_value_error():
    repo = _repo()
    with pytest.raises(ValueError):
        repo.history("1.1.1.1", limit=0)
    repo.close()


def test_history_limit_negative_raises_value_error():
    repo = _repo()
    with pytest.raises(ValueError):
        repo.history("1.1.1.1", limit=-1)
    repo.close()


def test_recent_also_rejects_invalid_limits_via_shared_history_logic():
    """recent() delegates to history() -- the same validation applies,
    not a separately (and possibly inconsistently) implemented check."""
    repo = _repo()
    with pytest.raises(ValueError):
        repo.recent("1.1.1.1", limit=0)
    repo.close()


# --- L. Domain object reconstruction ------------------------------------------------


def test_history_returns_raw_measurement_objects_with_correct_types():
    repo = _repo()
    repo.save(
        RawMeasurement(
            probe_type=ProbeType.DNS,
            target="1.1.1.1",
            timestamp=_day(1),
            success=False,
            error="dns failure",
            error_type=ProbeErrorType.DNS_FAILURE,
            extra={"attempted_resolvers": ["1.1.1.1", "8.8.8.8"]},
        )
    )
    (result,) = repo.history("1.1.1.1")
    assert isinstance(result, RawMeasurement)
    assert result.probe_type is ProbeType.DNS
    assert result.error_type is ProbeErrorType.DNS_FAILURE
    assert isinstance(result.timestamp, datetime)
    assert result.extra == {"attempted_resolvers": ["1.1.1.1", "8.8.8.8"]}
    repo.close()


# --- M. Failed measurements ---------------------------------------------------------


def test_history_includes_failed_measurements_by_default():
    repo = _repo()
    repo.save(_m(1, success=True))
    repo.save(_m(2, success=False))
    results = repo.history("1.1.1.1")
    assert len(results) == 2
    assert {r.success for r in results} == {True, False}
    repo.close()


def test_history_time_range_still_includes_failed_measurements():
    repo = _repo()
    repo.save(_m(2, success=False))
    results = repo.history("1.1.1.1", start=_day(1), end=_day(3))
    assert len(results) == 1
    assert results[0].success is False
    repo.close()


# --- N. Timezone preservation --------------------------------------------------------


def test_history_range_filtering_works_with_timezone_aware_boundaries():
    repo = _repo()
    ts = datetime(2026, 6, 15, 12, 30, tzinfo=timezone.utc)
    repo.save(RawMeasurement(probe_type=ProbeType.ICMP, target="1.1.1.1", timestamp=ts, success=True))

    results = repo.history(
        "1.1.1.1",
        start=ts - timedelta(minutes=1),
        end=ts + timedelta(minutes=1),
    )
    assert len(results) == 1
    assert results[0].timestamp == ts
    assert results[0].timestamp.tzinfo is not None
    repo.close()


def test_history_returned_timestamps_are_timezone_aware():
    repo = _repo()
    repo.save(_m(1))
    (result,) = repo.history("1.1.1.1")
    assert result.timestamp.tzinfo is not None
    repo.close()


# --- O. SQL parameterization / safety ------------------------------------------------


def test_history_target_with_sql_special_characters_does_not_break_filtering():
    """A target string containing quote/semicolon-like characters must
    be treated as an exact, literal value -- never interpolated into
    the SQL text (parameterized queries make this a behavioral, not
    just cosmetic, guarantee)."""
    repo = _repo()
    tricky_target = "weird'; DROP TABLE measurements; --"
    repo.save(_m(1, target=tricky_target))
    repo.save(_m(1, target="1.1.1.1"))

    results = repo.history(tricky_target)
    assert len(results) == 1
    assert results[0].target == tricky_target

    # And the "attack" target must not have affected the other rows or
    # dropped the table -- both are still queryable normally.
    assert len(repo.history("1.1.1.1")) == 1
    repo.close()


def test_history_query_uses_parameterized_sql_not_string_interpolation():
    """Static guard alongside the behavioral test above: the SQL text
    built inside history() must never contain an f-string/%-formatted/
    .format()-interpolated target value."""
    import inspect

    source = inspect.getsource(MeasurementRepository.history)
    # No string-formatting operators applied to the query text itself.
    assert 'f"SELECT' not in source
    assert "query.format(" not in source
    assert "% (" not in source
