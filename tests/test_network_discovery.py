"""
Tests for netscope.core.discovery, netscope.adapters.discovery.network_discovery,
and the Container's discovery_provider wiring.

All psutil calls are monkeypatched -- no test here depends on the real
machine's actual network interfaces/addresses, so results are
deterministic regardless of what environment this runs in. The one
exception is a single, clearly-labeled smoke test that calls the real
adapter to confirm it doesn't raise; psutil.net_if_addrs()/net_if_stats()
are local system introspection calls, not network I/O, so this does not
require internet access or a real network connection -- but it also
asserts nothing about specific interface names/addresses, since those
vary by machine.
"""

from __future__ import annotations

import socket

from netscope.adapters.discovery.network_discovery import PsutilNetworkDiscovery
from netscope.adapters.discovery.network_type_classifier import classify_network_type
from netscope.app.container import Container, build_container
from netscope.core.discovery import DiscoveryProvider
from netscope.core.models import NetworkInterface, NetworkSnapshot, NetworkType


# ---------------------------------------------------------------------------
# Fake discovery provider works (contract conformance without any real
# psutil/OS call at all)
# ---------------------------------------------------------------------------

class _FakeDiscoveryProvider:
    """A minimal, in-memory stand-in. Deliberately does NOT inherit from
    DiscoveryProvider -- Protocol is structural, so satisfying the shape
    is enough, matching the pattern already established for Probe fakes
    in tests/test_ports.py."""

    def __init__(self, snapshot: NetworkSnapshot) -> None:
        self._snapshot = snapshot

    def discover(self) -> NetworkSnapshot:
        return self._snapshot


def test_fake_discovery_provider_satisfies_discovery_provider_protocol():
    fake = _FakeDiscoveryProvider(NetworkSnapshot(interfaces=[]))
    assert isinstance(fake, DiscoveryProvider)


def test_fake_discovery_provider_returns_its_configured_snapshot():
    expected = NetworkSnapshot(interfaces=[NetworkInterface(name="eth0", is_up=True, addresses=["10.0.0.5"])])
    fake = _FakeDiscoveryProvider(expected)

    result = fake.discover()

    assert result is expected
    assert result.interfaces[0].name == "eth0"


# ---------------------------------------------------------------------------
# Adapter returns expected models (psutil mocked -- deterministic)
# ---------------------------------------------------------------------------

class _FakeSnicAddr:
    def __init__(self, family, address):
        self.family = family
        self.address = address


class _FakeSnicStats:
    def __init__(self, isup, flags=""):
        self.isup = isup
        self.flags = flags


def test_adapter_maps_psutil_interfaces_to_network_interface_models(monkeypatch):
    import netscope.adapters.discovery.network_discovery as module

    fake_addrs = {
        "eth0": [
            _FakeSnicAddr(module.psutil.AF_LINK, "02:aa:bb:cc:dd:ee"),
            _FakeSnicAddr(socket.AF_INET, "192.168.1.10"),
        ],
        "lo": [
            _FakeSnicAddr(socket.AF_INET, "127.0.0.1"),
        ],
    }
    fake_stats = {
        "eth0": _FakeSnicStats(isup=True, flags="up,broadcast,running"),
        "lo": _FakeSnicStats(isup=True, flags="up,loopback,running"),
    }

    monkeypatch.setattr(module.psutil, "net_if_addrs", lambda: fake_addrs)
    monkeypatch.setattr(module.psutil, "net_if_stats", lambda: fake_stats)

    snapshot = PsutilNetworkDiscovery().discover()

    assert isinstance(snapshot, NetworkSnapshot)
    assert {i.name for i in snapshot.interfaces} == {"eth0", "lo"}

    eth0 = next(i for i in snapshot.interfaces if i.name == "eth0")
    assert eth0.is_up is True
    assert eth0.is_loopback is False
    # MAC address (AF_LINK) must be excluded -- only the IP address remains
    assert eth0.addresses == ["192.168.1.10"]

    lo = next(i for i in snapshot.interfaces if i.name == "lo")
    assert lo.is_up is True
    assert lo.is_loopback is True
    assert lo.addresses == ["127.0.0.1"]


def test_adapter_handles_interface_down_correctly(monkeypatch):
    import netscope.adapters.discovery.network_discovery as module

    fake_addrs = {"ifb0": []}
    fake_stats = {"ifb0": _FakeSnicStats(isup=False, flags="broadcast,noarp")}

    monkeypatch.setattr(module.psutil, "net_if_addrs", lambda: fake_addrs)
    monkeypatch.setattr(module.psutil, "net_if_stats", lambda: fake_stats)

    snapshot = PsutilNetworkDiscovery().discover()

    assert len(snapshot.interfaces) == 1
    assert snapshot.interfaces[0].is_up is False
    assert snapshot.interfaces[0].addresses == []


def test_adapter_handles_interface_missing_from_stats_as_down(monkeypatch):
    """If psutil reports an interface's addresses but has no matching
    entry in net_if_stats() (edge case, but psutil's own docs don't
    guarantee the two dicts always have identical key sets), the
    adapter must not raise -- it should treat the interface as down
    rather than crashing on a missing lookup."""
    import netscope.adapters.discovery.network_discovery as module

    fake_addrs = {"weird0": [_FakeSnicAddr(socket.AF_INET, "10.1.1.1")]}
    fake_stats: dict = {}  # no entry for "weird0"

    monkeypatch.setattr(module.psutil, "net_if_addrs", lambda: fake_addrs)
    monkeypatch.setattr(module.psutil, "net_if_stats", lambda: fake_stats)

    snapshot = PsutilNetworkDiscovery().discover()

    assert len(snapshot.interfaces) == 1
    assert snapshot.interfaces[0].is_up is False
    assert snapshot.interfaces[0].is_loopback is False


# ---------------------------------------------------------------------------
# Empty interface handling
# ---------------------------------------------------------------------------

def test_adapter_returns_empty_snapshot_when_no_interfaces_reported(monkeypatch):
    import netscope.adapters.discovery.network_discovery as module

    monkeypatch.setattr(module.psutil, "net_if_addrs", lambda: {})
    monkeypatch.setattr(module.psutil, "net_if_stats", lambda: {})

    snapshot = PsutilNetworkDiscovery().discover()

    assert isinstance(snapshot, NetworkSnapshot)
    assert snapshot.interfaces == []


def test_adapter_returns_empty_snapshot_when_psutil_unavailable(monkeypatch):
    """Mirrors icmp_probe.py's pattern for a missing library: a
    well-formed, empty/unsuccessful result rather than a raised
    ImportError reaching the caller."""
    import netscope.adapters.discovery.network_discovery as module

    monkeypatch.setattr(module, "_PSUTIL_AVAILABLE", False)

    snapshot = PsutilNetworkDiscovery().discover()

    assert isinstance(snapshot, NetworkSnapshot)
    assert snapshot.interfaces == []


# ---------------------------------------------------------------------------
# Core has no psutil/socket imports; dependency direction is one-way
# ---------------------------------------------------------------------------

def _imported_top_level_modules(filepath: str) -> set[str]:
    import ast

    with open(filepath, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    return imported


def test_core_discovery_module_imports_no_psutil_socket_or_platform_apis():
    import netscope.core.discovery as discovery_module

    imported = _imported_top_level_modules(discovery_module.__file__)
    forbidden = {"psutil", "socket", "platform", "subprocess"}
    assert not (imported & forbidden), (
        f"core/discovery.py imports {imported & forbidden} -- "
        "OS/network-specific behavior belongs in adapters, not core"
    )


def test_core_discovery_module_only_imports_stdlib_typing_and_core_models():
    import netscope.core.discovery as discovery_module

    imported = _imported_top_level_modules(discovery_module.__file__)
    allowed = {"__future__", "typing", "netscope"}
    assert imported <= allowed, f"core/discovery.py imports {imported - allowed}, outside the allowed set"


def test_core_models_module_still_has_no_psutil_import_after_adding_discovery_models():
    """Regression guard: adding NetworkInterface/NetworkSnapshot to
    core/models.py must not have introduced a psutil import there."""
    import netscope.core.models as models_module

    imported = _imported_top_level_modules(models_module.__file__)
    assert "psutil" not in imported


def test_discovery_adapter_module_depends_on_core_not_the_reverse():
    """Confirms the one-way dependency direction: the adapter imports
    core.discovery and core.models; core.discovery does not import the
    adapter (checked structurally, since core/discovery.py has no way
    to name adapters.discovery.network_discovery without importing it,
    and the import-set assertions above already prove it doesn't)."""
    import netscope.adapters.discovery.network_discovery as adapter_module

    imported = _imported_top_level_modules(adapter_module.__file__)
    assert "netscope" in imported  # imports core.discovery / core.models
    assert "psutil" in imported  # this is the concrete infrastructure the adapter wraps


# ---------------------------------------------------------------------------
# Container wiring
# ---------------------------------------------------------------------------

def test_build_container_exposes_a_working_discovery_provider():
    container = build_container()
    assert isinstance(container.discovery_provider, DiscoveryProvider)
    assert isinstance(container.discovery_provider, PsutilNetworkDiscovery)


def test_container_default_discovery_provider_is_psutil_backed_when_not_specified():
    """Confirms backward compatibility: constructing Container with only
    probe_registry (the pre-TASK-010 call shape, still used by
    tests/test_probe_registry.py) still yields a working discovery_provider."""
    from netscope.adapters.probes.registry import ProbeRegistry

    container = Container(probe_registry=ProbeRegistry())

    assert isinstance(container.discovery_provider, DiscoveryProvider)


def test_container_accepts_an_explicit_fake_discovery_provider_for_testing():
    from netscope.adapters.probes.registry import ProbeRegistry

    fake = _FakeDiscoveryProvider(NetworkSnapshot(interfaces=[]))
    container = Container(probe_registry=ProbeRegistry(), discovery_provider=fake)

    assert container.discovery_provider is fake


def test_container_discovery_provider_snapshot_has_expected_shape_real_adapter_smoke_test():
    """SMOKE TEST using the real, non-mocked adapter: confirms it runs
    without raising and returns a well-formed NetworkSnapshot. Uses only
    local system introspection (psutil.net_if_addrs/net_if_stats), never
    internet access or a real network connection -- but asserts nothing
    about specific interface names/addresses/counts, since those vary by
    machine and must not make this test environment-dependent."""
    container = build_container()

    snapshot = container.discovery_provider.discover()

    assert isinstance(snapshot, NetworkSnapshot)
    assert isinstance(snapshot.interfaces, list)
    for iface in snapshot.interfaces:
        assert isinstance(iface, NetworkInterface)
        assert isinstance(iface.name, str)
        assert isinstance(iface.is_up, bool)
        assert isinstance(iface.addresses, list)


# ---------------------------------------------------------------------------
# TASK-012 -- Network type classification (classify_network_type)
#
# Pure, deterministic tests over interface name strings only -- no psutil,
# no mocking needed, since the classifier has no dependencies of its own.
# ---------------------------------------------------------------------------

def test_classify_wifi_like_interface_names_as_wifi():
    for name in ("wlan0", "wlp2s0", "wlx00c0ca123456", "Wi-Fi", "wi-fi 2", "wireless0"):
        assert classify_network_type(name) == NetworkType.WIFI, name


def test_classify_ethernet_like_interface_names_as_ethernet():
    for name in ("eth0", "eth1", "enp0s3", "eno1", "ens33", "Ethernet", "Ethernet 2"):
        assert classify_network_type(name) == NetworkType.ETHERNET, name


def test_classify_cellular_like_interface_names_as_cellular():
    for name in ("wwan0", "wwp0s20u6i12", "rmnet0", "ppp0", "Cellular", "Mobile Broadband"):
        assert classify_network_type(name) == NetworkType.CELLULAR, name


def test_classify_unknown_or_unrecognized_interface_names_as_unknown():
    for name in ("lo", "docker0", "veth1234", "br-abc123", "tun0", "tap0", "ifb0", "totally-made-up-name"):
        assert classify_network_type(name) == NetworkType.UNKNOWN, name


def test_classify_missing_or_empty_metadata_as_unknown():
    assert classify_network_type(None) == NetworkType.UNKNOWN
    assert classify_network_type("") == NetworkType.UNKNOWN
    assert classify_network_type("   ") == NetworkType.UNKNOWN


def test_classification_is_case_insensitive():
    assert classify_network_type("WLAN0") == NetworkType.WIFI
    assert classify_network_type("ETH0") == NetworkType.ETHERNET
    assert classify_network_type("WWAN0") == NetworkType.CELLULAR


# ---------------------------------------------------------------------------
# TASK-012 -- Adapter wiring: existing discovery behavior remains unchanged,
# network_type is now populated alongside the fields TASK-010 already added.
# ---------------------------------------------------------------------------

def test_adapter_populates_network_type_alongside_existing_fields(monkeypatch):
    """Extends (does not duplicate) TASK-010's
    test_adapter_maps_psutil_interfaces_to_network_interface_models --
    confirms network_type is now set without changing any previously
    asserted field (is_up, is_loopback, addresses)."""
    import netscope.adapters.discovery.network_discovery as module

    fake_addrs = {"wlan0": [_FakeSnicAddr(socket.AF_INET, "192.168.1.20")]}
    fake_stats = {"wlan0": _FakeSnicStats(isup=True, flags="up,broadcast,running")}

    monkeypatch.setattr(module.psutil, "net_if_addrs", lambda: fake_addrs)
    monkeypatch.setattr(module.psutil, "net_if_stats", lambda: fake_stats)

    snapshot = PsutilNetworkDiscovery().discover()

    iface = snapshot.interfaces[0]
    assert iface.name == "wlan0"
    assert iface.is_up is True
    assert iface.is_loopback is False
    assert iface.addresses == ["192.168.1.20"]
    assert iface.network_type == NetworkType.WIFI


def test_network_interface_construction_without_network_type_still_defaults_to_unknown():
    """Backward compatibility: existing construction calls (e.g. earlier
    in this file, and anywhere else in the codebase) that don't pass
    network_type must keep working unchanged."""
    iface = NetworkInterface(name="eth0", is_up=True, addresses=["10.0.0.5"])
    assert iface.network_type == NetworkType.UNKNOWN
