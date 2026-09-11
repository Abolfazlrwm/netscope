"""
Tests for netscope.app.config (TASK-035).

Fully offline: uses pytest's tmp_path fixture for any file-based test,
never touches ~/.netscope/config.toml.
"""

from __future__ import annotations

from netscope.app.config import NetScopeConfig, load_config


# ---------------------------------------------------------------------------
# Zero required configuration
# ---------------------------------------------------------------------------


def test_missing_config_file_returns_built_in_defaults(tmp_path):
    missing_path = tmp_path / "does-not-exist.toml"
    config = load_config(missing_path)
    assert config == NetScopeConfig()


def test_default_config_has_sensible_built_in_values():
    config = NetScopeConfig()
    assert config.public_dns_target == "1.1.1.1"
    assert config.dns_lookup_domain == "example.com"
    assert config.public_cdn_url == "https://www.cloudflare.com/"
    assert config.gateway is None


# ---------------------------------------------------------------------------
# Loading from a real TOML file
# ---------------------------------------------------------------------------


def test_load_config_reads_values_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'public_dns_target = "8.8.8.8"\n'
        'dns_lookup_domain = "netscope.example"\n'
        'public_cdn_url = "https://cdn.example.com/"\n'
        'gateway = "192.168.1.1"\n'
    )
    config = load_config(path)
    assert config.public_dns_target == "8.8.8.8"
    assert config.dns_lookup_domain == "netscope.example"
    assert config.public_cdn_url == "https://cdn.example.com/"
    assert config.gateway == "192.168.1.1"


def test_load_config_with_a_partial_file_keeps_defaults_for_missing_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('gateway = "10.0.0.1"\n')
    config = load_config(path)
    assert config.gateway == "10.0.0.1"
    assert config.public_dns_target == "1.1.1.1"  # untouched default
    assert config.dns_lookup_domain == "example.com"  # untouched default


def test_load_config_with_an_empty_file_returns_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    config = load_config(path)
    assert config == NetScopeConfig()


# ---------------------------------------------------------------------------
# Unknown keys are ignored, not fatal
# ---------------------------------------------------------------------------


def test_load_config_ignores_unknown_keys_rather_than_raising(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('public_dns_target = "8.8.8.8"\nsome_future_unknown_key = "whatever"\n')
    config = load_config(path)  # must not raise
    assert config.public_dns_target == "8.8.8.8"


def test_load_config_with_only_unknown_keys_returns_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('totally_unrecognized = 123\n')
    config = load_config(path)
    assert config == NetScopeConfig()


# ---------------------------------------------------------------------------
# No global/singleton state
# ---------------------------------------------------------------------------


def test_two_separately_loaded_configs_are_independent_objects(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('gateway = "10.0.0.1"\n')
    a = load_config(path)
    b = load_config(path)
    assert a == b
    assert a is not b


def test_mutating_one_loaded_config_does_not_affect_another(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('gateway = "10.0.0.1"\n')
    a = load_config(path)
    b = load_config(path)
    a.gateway = "192.168.0.1"
    assert b.gateway == "10.0.0.1"
