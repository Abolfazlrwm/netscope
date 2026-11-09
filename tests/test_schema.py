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
    """Exactly the schema's tables -- every one maps to an actual
    existing core domain model, nothing speculative (the v3 additions
    are the curated services list; see schema.py's module docstring
    for the table's own deferral history)."""
    conn = _fresh_connection()
    initialize_database(conn)
    assert _table_names(conn) == {
        "measurements",
        "route_snapshots",
        "route_hops",
        "metric_baselines",
        "throughput_baselines",
        "services",
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
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
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


def test_throughput_baselines_table_exists_after_initialization():
    """The v2-era throughput baseline table (current-version claims
    live in the vX_is_the_current_schema_version tests, one per era)."""
    conn = _fresh_connection()
    initialize_database(conn)
    assert "throughput_baselines" in _table_names(conn)
    conn.close()


def test_v1_database_upgrades_additively(tmp_path):
    """A v1 database (what v0.2/v0.3 users have on disk) upgrades in
    place: the throughput table appears, user_version moves to 2, and
    every pre-existing row is untouched."""
    conn = create_connection(tmp_path / "upgrade.db")
    conn.execute("INSERT INTO metric_baselines (target, metric_type, count, mean, m2) VALUES ('gw', 'latency', 5, 10.0, 8.0)")
    # Simulate the v1 era: no throughput table, user_version rewound.
    conn.execute("DROP TABLE throughput_baselines")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    initialize_database(conn)  # what a v0.4 binary does on startup

    assert get_schema_version(conn) == SCHEMA_VERSION
    assert "throughput_baselines" in _table_names(conn)
    row = conn.execute("SELECT count, mean FROM metric_baselines WHERE target = 'gw'").fetchone()
    assert row == (5, 10.0)
    conn.close()


def test_v3_is_the_current_schema_version_with_the_services_table():
    conn = _fresh_connection()
    initialize_database(conn)
    assert SCHEMA_VERSION == 3
    assert get_schema_version(conn) == SCHEMA_VERSION
    assert "services" in _table_names(conn)
    conn.close()


def test_services_table_has_the_documented_shape():
    """name is the primary key (the curated list is edited by re-saving
    under the same name), host is required, and checks defaults to ''
    -- no checks enabled yet, "each optional" per the Service model."""
    conn = _fresh_connection()
    initialize_database(conn)
    info = conn.execute("PRAGMA table_info(services)").fetchall()
    by_name = {row[1]: row for row in info}
    assert by_name["name"][5] == 1  # pk column flag
    assert by_name["host"][3] == 1  # notnull
    assert by_name["checks"][3] == 1  # notnull
    assert by_name["checks"][4] == "''"  # dflt_value: SQLite stores the literal default text: SQLite stores the literal default text

    conn.execute("INSERT INTO services (name, host) VALUES ('My VPN', 'vpn.example.net')")
    row = conn.execute("SELECT name, host, checks FROM services").fetchone()
    assert row == ("My VPN", "vpn.example.net", "")
    conn.close()


def test_v2_database_upgrades_additively(tmp_path):
    """A v2 database (what v0.4-v0.6 users have on disk) upgrades in
    place: the services table appears, user_version moves to 3, and
    every pre-existing row is untouched."""
    conn = create_connection(tmp_path / "upgrade.db")
    conn.execute("INSERT INTO measurements (probe_type, target, timestamp, success) VALUES ('icmp', 'gw', '2026-11-09T08:00:00+00:00', 1)")
    conn.execute("INSERT INTO metric_baselines (target, metric_type, count, mean, m2) VALUES ('gw', 'latency', 5, 10.0, 8.0)")
    # Simulate the v2 era: no services table, user_version rewound.
    conn.execute("DROP TABLE services")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()

    initialize_database(conn)  # what a v0.7 binary does on startup

    assert get_schema_version(conn) == SCHEMA_VERSION
    assert "services" in _table_names(conn)
    measurement = conn.execute("SELECT target, success FROM measurements").fetchone()
    assert measurement == ("gw", 1)
    baseline = conn.execute("SELECT count, mean FROM metric_baselines WHERE target = 'gw'").fetchone()
    assert baseline == (5, 10.0)
    conn.close()


def test_connections_allow_cross_thread_use_for_tui_workers():
    """The TUI's shell builds its container inside one worker thread and
    its screens use the same repositories from others -- the connection
    must tolerate sequential cross-thread use (see create_connection's
    docstring), not raise ProgrammingError the moment a second worker
    touches it."""
    import threading

    # Via create_connection -- the one canonical path (the fix lives
    # there, not in raw sqlite3.connect calls tests happen to make).
    conn = create_connection(":memory:")
    error: list[Exception] = []

    def _write_from_another_thread():
        try:
            with conn:
                conn.execute("INSERT INTO services (name, host, checks) VALUES ('thread test', 't.example.net', 'icmp')")
        except Exception as exc:  # noqa: BLE001 -- the assertion below reports it
            error.append(exc)

    writer = threading.Thread(target=_write_from_another_thread)
    writer.start()
    writer.join()

    assert error == [], f"cross-thread use raised: {error}"
    row = conn.execute("SELECT name, host FROM services").fetchone()
    assert row == ("thread test", "t.example.net")
    conn.close()
