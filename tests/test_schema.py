"""
Tests for netscope.persistence.schema (TASK-029).

Per module-boundaries.md's Persistence testing strategy: "Unit tests
against a temp-file or in-memory SQLite database (`sqlite3.connect(
":memory:")` or `tempfile`) -- no real filesystem path, no network."

All tests here use `:memory:` databases -- fully offline, deterministic,
and leave nothing behind on disk.
"""

from __future__ import annotations

import ast
import sqlite3

import pytest

from netscope.persistence.schema import SCHEMA_VERSION, create_connection, get_schema_version, initialize_database


def _fresh_connection() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


# ---------------------------------------------------------------------------
# 1. Fresh database initialization
# ---------------------------------------------------------------------------


def test_fresh_database_initializes_without_error():
    conn = _fresh_connection()
    initialize_database(conn)  # must not raise
    conn.close()


def test_a_brand_new_never_initialized_sqlite_file_reports_version_zero():
    """SQLite's own default for PRAGMA user_version -- confirms we're
    reading the real mechanism, not something we invented."""
    conn = _fresh_connection()
    assert get_schema_version(conn) == 0
    conn.close()


# ---------------------------------------------------------------------------
# 2. Schema version
# ---------------------------------------------------------------------------


def test_schema_version_is_recorded_after_initialization():
    conn = _fresh_connection()
    initialize_database(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_schema_version_constant_is_a_positive_integer():
    assert isinstance(SCHEMA_VERSION, int)
    assert SCHEMA_VERSION >= 1


# ---------------------------------------------------------------------------
# 3. Idempotency
# ---------------------------------------------------------------------------


def test_initializing_twice_does_not_raise():
    conn = _fresh_connection()
    initialize_database(conn)
    initialize_database(conn)  # must not raise
    conn.close()


def test_initializing_twice_does_not_change_the_version():
    conn = _fresh_connection()
    initialize_database(conn)
    initialize_database(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_initializing_twice_does_not_duplicate_tables():
    conn = _fresh_connection()
    initialize_database(conn)
    tables_first = _table_names(conn)
    initialize_database(conn)
    tables_second = _table_names(conn)
    assert tables_first == tables_second


# ---------------------------------------------------------------------------
# 4. Required tables
# ---------------------------------------------------------------------------


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows if r[0] != "sqlite_sequence"}


def test_expected_tables_exist_after_initialization():
    """Only tables that map to an actual existing core domain model --
    no speculative tables (e.g. no 'services' table; see schema.py's
    module docstring for why)."""
    conn = _fresh_connection()
    initialize_database(conn)
    assert _table_names(conn) == {
        "measurements",
        "route_snapshots",
        "route_hops",
        "metric_baselines",
        "evidence",
        "diagnoses",
        "diagnosis_evidence",
        "diagnosis_ruled_out",
        "incidents",
        "incident_evidence",
    }
    conn.close()


def test_measurements_table_has_the_error_type_column():
    """Regression guard for the one genuine (non-speculative) gap this
    task fixes: RawMeasurement.error_type had no column in the
    pre-existing sqlite_store.py schema."""
    conn = _fresh_connection()
    initialize_database(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(measurements)").fetchall()}
    assert "error_type" in columns
    conn.close()


# ---------------------------------------------------------------------------
# 5. Data preservation across repeated initialization
# ---------------------------------------------------------------------------


def test_existing_measurement_rows_survive_repeated_initialization():
    conn = _fresh_connection()
    initialize_database(conn)
    conn.execute(
        "INSERT INTO measurements (probe_type, target, timestamp, success, latency_ms) VALUES (?, ?, ?, ?, ?)",
        ("icmp", "1.1.1.1", "2026-01-01T00:00:00+00:00", 1, 12.5),
    )
    conn.commit()

    initialize_database(conn)  # re-run initialization

    rows = conn.execute("SELECT target, latency_ms FROM measurements").fetchall()
    assert rows == [("1.1.1.1", 12.5)]
    conn.close()


def test_existing_incident_rows_survive_repeated_initialization():
    conn = _fresh_connection()
    initialize_database(conn)
    conn.execute(
        "INSERT INTO incidents (started_at, ended_at, severity, target) VALUES (?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", None, "critical", "1.1.1.1"),
    )
    conn.commit()

    initialize_database(conn)

    rows = conn.execute("SELECT target, severity FROM incidents").fetchall()
    assert rows == [("1.1.1.1", "critical")]
    conn.close()


# ---------------------------------------------------------------------------
# 6. Foreign-key behavior
# ---------------------------------------------------------------------------


def test_foreign_keys_are_enforced():
    """route_hops.route_snapshot_id must reference a real row -- this is
    a genuine relationship (an ordered list of hops belonging to one
    snapshot), not a speculative constraint."""
    conn = _fresh_connection()
    initialize_database(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO route_hops (route_snapshot_id, hop_index, ttl, packet_loss_pct) VALUES (?, ?, ?, ?)",
            (999, 0, 1, 0.0),
        )
        conn.commit()
    conn.close()


def test_foreign_keys_allow_a_valid_referenced_row():
    conn = _fresh_connection()
    initialize_database(conn)
    conn.execute("INSERT INTO route_snapshots (target, timestamp) VALUES (?, ?)", ("1.1.1.1", "2026-01-01T00:00:00+00:00"))
    snapshot_id = conn.execute("SELECT id FROM route_snapshots").fetchone()[0]
    conn.execute(
        "INSERT INTO route_hops (route_snapshot_id, hop_index, ttl, packet_loss_pct) VALUES (?, ?, ?, ?)",
        (snapshot_id, 0, 1, 0.0),
    )
    conn.commit()  # must not raise
    conn.close()


def test_evidence_source_check_constraint_rejects_both_sources_set():
    """Evidence.source is Optional[RawMeasurement | RouteSnapshot] -- at
    most one, never both."""
    conn = _fresh_connection()
    initialize_database(conn)
    conn.execute("INSERT INTO measurements (probe_type, target, timestamp, success) VALUES ('icmp','1.1.1.1','2026-01-01T00:00:00',1)")
    conn.execute("INSERT INTO route_snapshots (target, timestamp) VALUES ('1.1.1.1','2026-01-01T00:00:00')")
    m_id = conn.execute("SELECT id FROM measurements").fetchone()[0]
    r_id = conn.execute("SELECT id FROM route_snapshots").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO evidence
                (metric, tested, severity, confidence, source_measurement_id, source_route_snapshot_id)
            VALUES ('gateway_latency', 1, 'info', 1.0, ?, ?)
            """,
            (m_id, r_id),
        )
        conn.commit()
    conn.close()


def test_diagnosis_ruled_out_check_constraint_requires_exactly_one_of_evidence_or_hypothesis():
    conn = _fresh_connection()
    initialize_database(conn)
    conn.execute("INSERT INTO diagnoses (classification, confidence, timestamp) VALUES ('dns_issue', 0.8, '2026-01-01T00:00:00')")
    diagnosis_id = conn.execute("SELECT id FROM diagnoses").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO diagnosis_ruled_out (diagnosis_id, evidence_id, hypothesis) VALUES (?, NULL, NULL)",
            (diagnosis_id,),
        )
        conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# create_connection convenience
# ---------------------------------------------------------------------------


def test_create_connection_initializes_an_in_memory_database():
    conn = create_connection(":memory:")
    assert get_schema_version(conn) == SCHEMA_VERSION
    assert "measurements" in _table_names(conn)
    conn.close()


def test_create_connection_creates_parent_directory_for_a_file_path(tmp_path):
    db_path = tmp_path / "nested" / "netscope.db"
    conn = create_connection(db_path)
    assert db_path.exists()
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


# ---------------------------------------------------------------------------
# 7. No core SQLite dependency
# ---------------------------------------------------------------------------


def _imports_of(module) -> set[str]:
    tree = ast.parse(open(module.__file__).read())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_core_diagnosis_does_not_import_sqlite3():
    import netscope.core.diagnosis as m

    assert "sqlite3" not in _imports_of(m)


def test_core_incidents_does_not_import_sqlite3():
    import netscope.core.incidents as m

    assert "sqlite3" not in _imports_of(m)


def test_core_models_does_not_import_sqlite3():
    import netscope.core.models as m

    assert "sqlite3" not in _imports_of(m)


def test_core_scoring_does_not_import_sqlite3():
    import netscope.core.scoring as m

    assert "sqlite3" not in _imports_of(m)


def test_core_baseline_does_not_import_sqlite3():
    import netscope.core.baseline as m

    assert "sqlite3" not in _imports_of(m)


def test_schema_module_only_imports_stdlib():
    """persistence/schema.py may use sqlite3 and pathlib freely (it IS
    the infrastructure layer) -- but must not import UI/adapters/probes,
    keeping dependency direction one-way."""
    import netscope.persistence.schema as m

    modules = _imports_of(m)
    assert modules <= {"__future__", "sqlite3", "pathlib"}
