"""
Tests for the v0.2 adapters: default-gateway detection (platform
parsers + policy) and bounded concurrent hostname resolution.
"""

from __future__ import annotations

from netscope.adapters.discovery.gateway_detector import (
    GatewayDetector,
    _detect_linux,
    _detect_macos,
    _detect_windows,
    detect_default_gateway,
)
from netscope.adapters.discovery.hostname_resolver import HostnameResolver

# ---------------------------------------------------------------------------
# Gateway detection -- parser-level tests (no real OS calls)
# ---------------------------------------------------------------------------


def test_linux_parser_extracts_default_gateway(monkeypatch, tmp_path):
    proc = tmp_path / "route"
    proc.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
        "wlan0\t0001A8C0\t00000000\t0003\t0\t0\t600\tFFF00000\t0\t0\t0\n"
    )
    import netscope.adapters.discovery.gateway_detector as gd

    monkeypatch.setattr(gd, "_PROC_NET_ROUTE", proc)
    assert _detect_linux() == "192.168.1.1"


def test_linux_parser_returns_none_without_default_route(monkeypatch, tmp_path):
    proc = tmp_path / "route"
    proc.write_text("Iface\tDestination\tGateway\tFlags\nwlan0\t0001A8C0\t00000000\t0003\n")
    import netscope.adapters.discovery.gateway_detector as gd

    monkeypatch.setattr(gd, "_PROC_NET_ROUTE", proc)
    assert _detect_linux() is None


def test_linux_parser_survives_missing_proc_file(monkeypatch, tmp_path):
    import netscope.adapters.discovery.gateway_detector as gd

    monkeypatch.setattr(gd, "_PROC_NET_ROUTE", tmp_path / "does-not-exist")
    assert _detect_linux() is None


def test_macos_parser_extracts_gateway(monkeypatch):
    import netscope.adapters.discovery.gateway_detector as gd

    class FakeCompleted:
        stdout = "   route to: default\ndestination: default\n    gateway: 192.168.1.254\n  interface: en0\n"

    monkeypatch.setattr(gd.shutil, "which", lambda name: "/sbin/route")
    monkeypatch.setattr(gd.subprocess, "run", lambda *a, **kw: FakeCompleted())
    assert _detect_macos() == "192.168.1.254"


def test_windows_parser_extracts_gateway(monkeypatch):
    import netscope.adapters.discovery.gateway_detector as gd

    class FakeCompleted:
        stdout = (
            "Active Routes:\n"
            "Network Destination        Netmask          Gateway       Interface  Metric\n"
            "          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.100     25\n"
            "     192.168.1.0  255.255.255.0        On-link         192.168.1.100    281\n"
        )

    monkeypatch.setattr(gd.shutil, "which", lambda name: "C:\\Windows\\route.exe")
    monkeypatch.setattr(gd.subprocess, "run", lambda *a, **kw: FakeCompleted())
    assert _detect_windows() == "192.168.1.1"


def test_detector_injected_strategy_short_circuits_platform_detection():
    detector = GatewayDetector(strategy=lambda: "10.9.9.9")
    assert detector.detect() == "10.9.9.9"


def test_detect_default_gateway_never_raises():
    # Whatever the sandbox platform reports (or fails to), the contract
    # is a str-or-None, never an exception.
    result = detect_default_gateway()
    assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# Hostname resolution -- bounded, concurrent, never raising
# ---------------------------------------------------------------------------


def test_resolver_resolves_localhost_addresses(monkeypatch):
    import socket

    def localhost_ptr(addr):
        return ("localhost", [], [addr])

    # macOS runners may legitimately report no PTR record for 127.0.0.1 —
    # patch the resolver's real dependency so the test stays deterministic
    # and fully offline on every platform.
    monkeypatch.setattr(socket, "gethostbyaddr", localhost_ptr)
    resolver = HostnameResolver(timeout=3.0)
    results = resolver.resolve_all(["127.0.0.1"])
    assert results["127.0.0.1"] == "localhost"


def test_resolver_handles_unresolvable_and_none_addresses():
    resolver = HostnameResolver(timeout=2.0)
    # TEST-NET-3 (RFC 5737) has no reverse DNS; None addresses are skipped.
    results = resolver.resolve_all([None, "203.0.113.1"])
    assert results["203.0.113.1"] is None
    assert all(k != "None" for k in results)


def test_resolver_deduplicates_addresses():
    resolver = HostnameResolver(timeout=3.0)
    results = resolver.resolve_all(["127.0.0.1", "127.0.0.1", "127.0.0.1"])
    assert list(results.keys()) == ["127.0.0.1"]


def test_resolver_empty_input_returns_empty_mapping():
    resolver = HostnameResolver()
    assert resolver.resolve_all([]) == {}
    assert resolver.resolve_all([None, None]) == {}


def test_resolver_never_raises_on_socket_errors(monkeypatch):
    import netscope.adapters.discovery.hostname_resolver as hr

    def boom(addr):
        raise OSError("no network")

    monkeypatch.setattr(hr, "_resolve_one", boom)
    resolver = HostnameResolver(timeout=2.0)
    assert resolver.resolve_all(["127.0.0.1"]) == {"127.0.0.1": None}
