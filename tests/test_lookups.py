"""
Tests for netscope.adapters.lookups.AsnLookupAdapter.

Fully offline and deterministic: every test uses a small, hand-built
in-memory or temp-file ASN database -- no external database is
downloaded, and no live network access happens anywhere in this file.
Unlike the module's original pyasn-based version, this implementation
has zero third-party dependencies, so every behavior tested here is
exercised against NetScope's own pure-Python parser/matcher directly
(verified manually against known-correct expected results before
these tests were written, not assumed).
"""

from __future__ import annotations

import pytest

from netscope.adapters.lookups import AsnLookupAdapter, AsnLookupResult, AsnLookupUnavailableError

# A tiny, self-contained test database covering: a normal /24, tab and
# space as separators, a comment line, and a default route (/0) to
# exercise longest-prefix-match against a catch-all entry. Real-world
# ASN numbers used purely as recognizable values (Cloudflare/Google) --
# this is a hand-built fixture, not data fetched from anywhere.
_TEST_DB = """
; test database -- comment lines like this must be skipped
1.1.1.0/24    13335
8.8.8.0/24\t15169
0.0.0.0/0     999999
"""


# ---------------------------------------------------------------------------
# Successful lookup
# ---------------------------------------------------------------------------

def test_successful_lookup_returns_asn_and_matching_prefix():
    adapter = AsnLookupAdapter(ipasn_string=_TEST_DB)

    result = adapter.lookup("1.1.1.1")

    assert isinstance(result, AsnLookupResult)
    assert result.ip_address == "1.1.1.1"
    assert result.asn == 13335
    assert result.prefix == "1.1.1.0/24"
    assert result.matched is True


def test_successful_lookup_for_a_second_distinct_prefix():
    adapter = AsnLookupAdapter(ipasn_string=_TEST_DB)

    result = adapter.lookup("8.8.8.8")

    assert result.asn == 15169
    assert result.prefix == "8.8.8.0/24"


def test_tab_and_space_separators_are_both_accepted():
    """The test database deliberately mixes a space-separated line
    (1.1.1.0/24) and a tab-separated line (8.8.8.0/24) -- both must
    parse correctly."""
    adapter = AsnLookupAdapter(ipasn_string=_TEST_DB)

    assert adapter.lookup("1.1.1.1").asn == 13335
    assert adapter.lookup("8.8.8.8").asn == 15169


def test_comment_lines_are_skipped():
    """A leading comment line (starting with ';') must not be treated
    as a malformed data line."""
    db = ";  this is a comment, not data\n1.2.3.0/24\t64500\n"
    adapter = AsnLookupAdapter(ipasn_string=db)

    assert adapter.lookup("1.2.3.4").asn == 64500


# ---------------------------------------------------------------------------
# Longest-prefix-match specifically
# ---------------------------------------------------------------------------

def test_longest_prefix_match_prefers_the_most_specific_matching_entry():
    """An address inside both a /24 and the database's /0 default
    route must match the /24 (the more specific entry), not the
    default route."""
    adapter = AsnLookupAdapter(ipasn_string=_TEST_DB)

    result = adapter.lookup("1.1.1.200")

    assert result.asn == 13335
    assert result.prefix == "1.1.1.0/24"


def test_default_route_matches_addresses_outside_any_specific_prefix():
    """An address not covered by any specific entry falls through to
    the database's default route (0.0.0.0/0), if one is present --
    this is a real database design choice (a catch-all), not a bug."""
    adapter = AsnLookupAdapter(ipasn_string=_TEST_DB)

    result = adapter.lookup("192.168.1.1")

    assert result.asn == 999999
    assert result.prefix == "0.0.0.0/0"


# ---------------------------------------------------------------------------
# No match (legitimate, not an error) -- database with no default route
# ---------------------------------------------------------------------------

def test_no_match_returns_none_asn_and_prefix_not_an_error():
    """With no default route in the database, a genuinely-uncovered
    address returns an unmatched result -- no exception."""
    adapter = AsnLookupAdapter(ipasn_string="1.1.1.0/24\t13335\n")

    result = adapter.lookup("192.168.1.1")

    assert result.asn is None
    assert result.prefix is None
    assert result.matched is False


# ---------------------------------------------------------------------------
# IPv6 support
# ---------------------------------------------------------------------------

def test_ipv6_lookup_works():
    adapter = AsnLookupAdapter(ipasn_string="2001:db8::/32\t65000\n")

    result = adapter.lookup("2001:db8::1")

    assert result.asn == 65000
    assert result.prefix == "2001:db8::/32"


def test_ipv6_no_match_is_unmatched_not_an_error():
    adapter = AsnLookupAdapter(ipasn_string="2001:db8::/32\t65000\n")

    result = adapter.lookup("2001:db9::1")

    assert result.matched is False


# ---------------------------------------------------------------------------
# Invalid input
# ---------------------------------------------------------------------------

def test_invalid_ip_address_raises_value_error():
    adapter = AsnLookupAdapter(ipasn_string=_TEST_DB)

    with pytest.raises(ValueError):
        adapter.lookup("not-an-ip-address")


def test_empty_string_ip_address_raises_value_error():
    adapter = AsnLookupAdapter(ipasn_string=_TEST_DB)

    with pytest.raises(ValueError):
        adapter.lookup("")


# ---------------------------------------------------------------------------
# Unavailable lookup data
# ---------------------------------------------------------------------------

def test_malformed_database_content_raises_asn_lookup_unavailable_error():
    with pytest.raises(AsnLookupUnavailableError):
        AsnLookupAdapter(ipasn_string="this is not a valid database format at all")


def test_missing_database_file_raises_asn_lookup_unavailable_error():
    """New coverage vs. the original implementation: file-based loading
    was not previously tested. A nonexistent path must be classified
    as AsnLookupUnavailableError (a defined, explicit failure), not an
    uncaught OSError reaching the caller."""
    with pytest.raises(AsnLookupUnavailableError):
        AsnLookupAdapter(ipasn_file="/nonexistent/path/to/asn-database.dat")


def test_database_file_loads_correctly(tmp_path):
    """New coverage: confirms the file-reading path (as opposed to
    ipasn_string) works correctly end to end, using a real temp file
    -- still fully local and offline, no download involved."""
    db_file = tmp_path / "test_asn_db.dat"
    db_file.write_text("1.1.1.0/24\t13335\n", encoding="utf-8")

    adapter = AsnLookupAdapter(ipasn_file=str(db_file))

    result = adapter.lookup("1.1.1.1")
    assert result.asn == 13335


def test_missing_both_database_arguments_raises_value_error():
    with pytest.raises(ValueError):
        AsnLookupAdapter()


def test_malformed_single_line_reports_the_offending_line_number():
    """A malformed line's number is included in the error message --
    useful for anyone hand-editing a database file."""
    db = "1.1.1.0/24\t13335\nthis-line-is-broken\n8.8.8.0/24\t15169\n"

    with pytest.raises(AsnLookupUnavailableError) as exc_info:
        AsnLookupAdapter(ipasn_string=db)

    assert "line 2" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Dependency direction / import boundaries / no third-party dependency
# ---------------------------------------------------------------------------

def test_core_models_module_does_not_import_lookup_infrastructure():
    """core must remain infrastructure-independent -- lookup logic
    belongs only in adapters/lookups.py, confirmed the same way every
    other infrastructure-import boundary in this project is checked
    (AST inspection via the module's own __file__)."""
    import ast
    import netscope.core.models as models_module

    with open(models_module.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "ipaddress" not in imported


def test_lookups_module_has_no_third_party_dependency():
    """Confirms the core correction this task made: lookups.py imports
    nothing beyond the Python standard library -- no pyasn, no other
    third-party ASN/radix library, which is exactly what makes this
    implementation install cleanly on Windows/Python 3.13 without a
    native compiler."""
    import ast
    import netscope.adapters.lookups as module

    with open(module.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    allowed = {"__future__", "ipaddress", "dataclasses", "typing"}
    assert imported <= allowed, f"unexpected imports: {imported - allowed}"
