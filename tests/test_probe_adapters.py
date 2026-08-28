"""
Tests for netscope.adapters.probes.*.

These verify that each adapter:
- satisfies netscope.core.ports.Probe
- exposes the correct ProbeType
- returns a RawMeasurement
- faithfully delegates to (and returns exactly what is returned by) the
  existing, unmodified probe function -- both on success and on failure

Existing probe module functions (icmp_probe.ping, dns_probe.resolve,
http_probe.fetch) are monkeypatched at the module level so these tests
run fully offline and deterministically -- no real ICMP/DNS/HTTP/network
access is used, consistent with the rest of the test suite.
"""

from __future__ import annotations

from netscope.adapters.probes.dns_adapter import DNSProbeAdapter, _classify_dns_error
from netscope.adapters.probes.http_adapter import HTTPProbeAdapter
from netscope.adapters.probes.icmp_adapter import ICMPProbeAdapter, _classify_icmp_error
from netscope.adapters.probes.tcp_adapter import TCPProbeAdapter
from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement
from netscope.core.ports import Probe
from netscope.probes import dns_probe, http_probe, icmp_probe, tcp_probe


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_icmp_adapter_satisfies_probe_protocol():
    assert isinstance(ICMPProbeAdapter(), Probe)


def test_dns_adapter_satisfies_probe_protocol():
    assert isinstance(DNSProbeAdapter(), Probe)


def test_http_adapter_satisfies_probe_protocol():
    assert isinstance(HTTPProbeAdapter(), Probe)


# ---------------------------------------------------------------------------
# Correct ProbeType
# ---------------------------------------------------------------------------

def test_icmp_adapter_reports_icmp_probe_type():
    assert ICMPProbeAdapter().probe_type == ProbeType.ICMP


def test_dns_adapter_reports_dns_probe_type():
    assert DNSProbeAdapter().probe_type == ProbeType.DNS


def test_http_adapter_reports_http_probe_type():
    assert HTTPProbeAdapter().probe_type == ProbeType.HTTP


# ---------------------------------------------------------------------------
# Returns RawMeasurement, existing (successful) behavior preserved exactly
# ---------------------------------------------------------------------------

def test_icmp_adapter_returns_raw_measurement_and_forwards_target_and_options(monkeypatch):
    captured = {}

    def fake_ping(target, **kwargs):
        captured["target"] = target
        captured["kwargs"] = kwargs
        return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=True, latency_ms=12.3)

    monkeypatch.setattr(icmp_probe, "ping", fake_ping)

    result = ICMPProbeAdapter().run("1.1.1.1", count=2, timeout=1.0, privileged=False)

    assert isinstance(result, RawMeasurement)
    assert result.success is True
    assert result.latency_ms == 12.3
    assert captured["target"] == "1.1.1.1"
    assert captured["kwargs"] == {"count": 2, "timeout": 1.0, "privileged": False}


def test_dns_adapter_returns_raw_measurement_and_forwards_target_and_options(monkeypatch):
    captured = {}

    def fake_resolve(hostname, **kwargs):
        captured["hostname"] = hostname
        captured["kwargs"] = kwargs
        return RawMeasurement(probe_type=ProbeType.DNS, target=hostname, success=True, latency_ms=5.0)

    monkeypatch.setattr(dns_probe, "resolve", fake_resolve)

    result = DNSProbeAdapter().run("example.com", record_type="AAAA", resolver_ip="1.1.1.1", timeout=1.5)

    assert isinstance(result, RawMeasurement)
    assert result.success is True
    assert captured["hostname"] == "example.com"
    assert captured["kwargs"] == {"record_type": "AAAA", "resolver_ip": "1.1.1.1", "timeout": 1.5}


def test_http_adapter_returns_raw_measurement_and_forwards_target_and_options(monkeypatch):
    captured = {}

    def fake_fetch(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return RawMeasurement(probe_type=ProbeType.HTTP, target=url, success=True, latency_ms=42.0)

    monkeypatch.setattr(http_probe, "fetch", fake_fetch)

    result = HTTPProbeAdapter().run("https://example.com/", timeout=3.0)

    assert isinstance(result, RawMeasurement)
    assert result.success is True
    assert captured["url"] == "https://example.com/"
    assert captured["kwargs"] == {"timeout": 3.0}


def test_adapters_work_with_no_extra_options_using_underlying_defaults(monkeypatch):
    """Confirms **options is genuinely optional -- an adapter call with
    only a target still reaches the underlying function, which applies
    its own existing defaults (count=4, timeout=2.0, etc.), unchanged."""
    calls = []

    def fake_ping(target, **kwargs):
        calls.append((target, kwargs))
        return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=True)

    monkeypatch.setattr(icmp_probe, "ping", fake_ping)

    ICMPProbeAdapter().run("8.8.8.8")

    assert calls == [("8.8.8.8", {})]


# ---------------------------------------------------------------------------
# Failures handled correctly -- adapter passes through failure results
# exactly as the existing probe functions already produce them (those
# functions already catch their own exceptions internally and return a
# success=False RawMeasurement rather than raising -- the adapter must
# not swallow, alter, or reinterpret that result in any way)
# ---------------------------------------------------------------------------

def test_icmp_adapter_passes_through_failed_measurement_unchanged(monkeypatch):
    failed = RawMeasurement(
        probe_type=ProbeType.ICMP,
        target="10.0.0.1",
        success=False,
        error="permission error, unprivileged fallback also failed: [Errno 1] Operation not permitted",
    )
    monkeypatch.setattr(icmp_probe, "ping", lambda target, **kwargs: failed)

    result = ICMPProbeAdapter().run("10.0.0.1")

    assert result is failed
    assert result.success is False
    assert "Operation not permitted" in result.error


def test_dns_adapter_passes_through_failed_measurement_unchanged(monkeypatch):
    failed = RawMeasurement(
        probe_type=ProbeType.DNS,
        target="does-not-exist.invalid",
        success=False,
        error="NXDOMAIN",
    )
    monkeypatch.setattr(dns_probe, "resolve", lambda hostname, **kwargs: failed)

    result = DNSProbeAdapter().run("does-not-exist.invalid")

    assert result is failed
    assert result.success is False
    assert result.error == "NXDOMAIN"


def test_http_adapter_passes_through_failed_measurement_unchanged(monkeypatch):
    failed = RawMeasurement(
        probe_type=ProbeType.HTTP,
        target="https://example.com/",
        success=False,
        error="ConnectTimeout",
    )
    monkeypatch.setattr(http_probe, "fetch", lambda url, **kwargs: failed)

    result = HTTPProbeAdapter().run("https://example.com/")

    assert result is failed
    assert result.success is False
    assert result.error == "ConnectTimeout"


def test_icmp_adapter_does_not_add_its_own_error_handling(monkeypatch):
    """The existing icmp_probe.ping() already catches its own exceptions
    and never raises (per implementation-audit.md's characterization of
    current behavior). The adapter must stay a thin, transparent wrapper
    and must NOT add a second layer of try/except that could mask a
    genuine programming error differently than the underlying function
    already does -- so an unexpected exception from the underlying
    function must propagate through the adapter unmodified, not be
    caught and reinterpreted here."""

    def raising_ping(target, **kwargs):
        raise RuntimeError("unexpected programming error, not a normal probe failure")

    monkeypatch.setattr(icmp_probe, "ping", raising_ping)

    try:
        ICMPProbeAdapter().run("1.1.1.1")
        assert False, "expected RuntimeError to propagate through the adapter"
    except RuntimeError as exc:
        assert "unexpected programming error" in str(exc)


# ---------------------------------------------------------------------------
# Adapters contain no measurement logic of their own
# ---------------------------------------------------------------------------

def test_adapter_modules_do_not_import_network_libraries_directly():
    """Adapters delegate to the existing probes.* modules -- they must
    not import icmplib/dnspython/httpx themselves, which would indicate
    duplicated measurement logic rather than a thin wrapper."""
    import ast
    import netscope.adapters.probes.icmp_adapter as icmp_adapter
    import netscope.adapters.probes.dns_adapter as dns_adapter
    import netscope.adapters.probes.http_adapter as http_adapter

    for module in (icmp_adapter, dns_adapter, http_adapter):
        with open(module.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split(".")[0])

        forbidden = {"icmplib", "dns", "httpx"}
        assert not (imported_modules & forbidden), (
            f"{module.__name__} imports {imported_modules & forbidden} directly -- "
            "adapters must delegate to netscope.probes.*, not reimplement measurement logic"
        )


# ---------------------------------------------------------------------------
# TASK-014 -- Structured errors for ICMP (ProbeErrorType), scoped to ICMP
# only, classified from icmp_probe.py's existing free-text error strings.
# icmp_probe.py itself is not modified or exercised for real (no real
# ICMP), so these are fully offline and deterministic.
# ---------------------------------------------------------------------------

def test_icmp_adapter_successful_measurement_has_no_error_type(monkeypatch):
    monkeypatch.setattr(
        icmp_probe, "ping",
        lambda target, **kwargs: RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=True, latency_ms=5.0),
    )

    result = ICMPProbeAdapter().run("1.1.1.1")

    assert result.success is True
    assert result.error_type is None


def test_icmp_adapter_classifies_missing_library_as_probe_unavailable(monkeypatch):
    monkeypatch.setattr(
        icmp_probe, "ping",
        lambda target, **kwargs: RawMeasurement(
            probe_type=ProbeType.ICMP, target=target, success=False, error="icmplib not installed",
        ),
    )

    result = ICMPProbeAdapter().run("1.1.1.1")

    assert result.error_type == ProbeErrorType.PROBE_UNAVAILABLE


def test_icmp_adapter_classifies_permission_error_as_permission_denied(monkeypatch):
    monkeypatch.setattr(
        icmp_probe, "ping",
        lambda target, **kwargs: RawMeasurement(
            probe_type=ProbeType.ICMP,
            target=target,
            success=False,
            error="permission error, unprivileged fallback also failed: [Errno 1] Operation not permitted",
        ),
    )

    result = ICMPProbeAdapter().run("1.1.1.1")

    assert result.error_type == ProbeErrorType.PERMISSION_DENIED


def test_icmp_adapter_classifies_no_response_with_no_error_string_as_timeout(monkeypatch):
    """icmp_probe.py's ping() does not set `error` when icmplib.ping()
    completes without raising but the host never responds (100% packet
    loss, is_alive=False) -- this failed-with-no-error-string case is
    the timeout case."""
    monkeypatch.setattr(
        icmp_probe, "ping",
        lambda target, **kwargs: RawMeasurement(
            probe_type=ProbeType.ICMP, target=target, success=False, packet_loss_pct=100.0, error=None,
        ),
    )

    result = ICMPProbeAdapter().run("10.0.0.99")

    assert result.error_type == ProbeErrorType.TIMEOUT


def test_icmp_adapter_classifies_unrecognized_error_as_unknown(monkeypatch):
    monkeypatch.setattr(
        icmp_probe, "ping",
        lambda target, **kwargs: RawMeasurement(
            probe_type=ProbeType.ICMP, target=target, success=False, error="some totally different failure",
        ),
    )

    result = ICMPProbeAdapter().run("1.1.1.1")

    assert result.error_type == ProbeErrorType.UNKNOWN


def test_classify_icmp_error_helper_directly_covers_all_four_cases():
    """Direct unit tests of the classification function itself, not
    just through the adapter -- keeps the mapping's correctness visible
    without needing to reconstruct a full adapter call each time."""
    assert _classify_icmp_error(
        RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=True)
    ) is None
    assert _classify_icmp_error(
        RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=False, error="icmplib not installed")
    ) == ProbeErrorType.PROBE_UNAVAILABLE
    assert _classify_icmp_error(
        RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=False, error="Permission denied")
    ) == ProbeErrorType.PERMISSION_DENIED
    assert _classify_icmp_error(
        RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=False, error=None)
    ) == ProbeErrorType.TIMEOUT
    assert _classify_icmp_error(
        RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=False, error="weird")
    ) == ProbeErrorType.UNKNOWN


# ---------------------------------------------------------------------------
# TASK-015 -- Structured errors for DNS (same migration pattern as
# TASK-014's ICMP), classified from dns_probe.py's existing free-text
# error strings. dns_probe.py itself is not modified or exercised for
# real (dns.resolver is monkeypatched at the dns_probe.resolve() level,
# matching the established convention from TASK-007/014), so these are
# fully offline and deterministic.
# ---------------------------------------------------------------------------

def test_dns_adapter_successful_measurement_has_no_error_type(monkeypatch):
    monkeypatch.setattr(
        dns_probe, "resolve",
        lambda hostname, **kwargs: RawMeasurement(probe_type=ProbeType.DNS, target=hostname, success=True, latency_ms=5.0),
    )

    result = DNSProbeAdapter().run("example.com")

    assert result.success is True
    assert result.error_type is None


def test_dns_adapter_classifies_missing_library_as_probe_unavailable(monkeypatch):
    monkeypatch.setattr(
        dns_probe, "resolve",
        lambda hostname, **kwargs: RawMeasurement(
            probe_type=ProbeType.DNS, target=hostname, success=False, error="dnspython not installed",
        ),
    )

    result = DNSProbeAdapter().run("example.com")

    assert result.error_type == ProbeErrorType.PROBE_UNAVAILABLE


def test_dns_adapter_classifies_timeout_as_timeout(monkeypatch):
    """Matches dns.exception.Timeout's actual string representation,
    'The DNS operation timed out.' (verified directly against the
    installed dnspython package)."""
    monkeypatch.setattr(
        dns_probe, "resolve",
        lambda hostname, **kwargs: RawMeasurement(
            probe_type=ProbeType.DNS, target=hostname, success=False, error="The DNS operation timed out.",
        ),
    )

    result = DNSProbeAdapter().run("example.com")

    assert result.error_type == ProbeErrorType.TIMEOUT


def test_dns_adapter_classifies_nxdomain_as_dns_failure(monkeypatch):
    """Matches dns.resolver.NXDOMAIN's actual string representation,
    'The DNS query name does not exist.' -- and any other
    dnspython-raised resolution failure (NoAnswer, NoNameservers, etc.)
    falls into the same DNS_FAILURE bucket, since none of them fit the
    ICMP-scoped TIMEOUT/PERMISSION_DENIED/PROBE_UNAVAILABLE values."""
    monkeypatch.setattr(
        dns_probe, "resolve",
        lambda hostname, **kwargs: RawMeasurement(
            probe_type=ProbeType.DNS, target=hostname, success=False, error="The DNS query name does not exist.",
        ),
    )

    result = DNSProbeAdapter().run("does-not-exist.invalid")

    assert result.error_type == ProbeErrorType.DNS_FAILURE


def test_classify_dns_error_helper_directly_covers_all_cases():
    """Direct unit tests of the classification function itself, not
    just through the adapter -- mirrors the equivalent ICMP test."""
    assert _classify_dns_error(
        RawMeasurement(probe_type=ProbeType.DNS, target="x", success=True)
    ) is None
    assert _classify_dns_error(
        RawMeasurement(probe_type=ProbeType.DNS, target="x", success=False, error="dnspython not installed")
    ) == ProbeErrorType.PROBE_UNAVAILABLE
    assert _classify_dns_error(
        RawMeasurement(probe_type=ProbeType.DNS, target="x", success=False, error="The DNS operation timed out.")
    ) == ProbeErrorType.TIMEOUT
    assert _classify_dns_error(
        RawMeasurement(probe_type=ProbeType.DNS, target="x", success=False, error="NXDOMAIN or whatever else dnspython says")
    ) == ProbeErrorType.DNS_FAILURE
    assert _classify_dns_error(
        RawMeasurement(probe_type=ProbeType.DNS, target="x", success=False, error=None)
    ) == ProbeErrorType.UNKNOWN


# ---------------------------------------------------------------------------
# TASK-016 -- New TCP connect-timing probe (stdlib socket only). Unlike
# ICMP/DNS, there is no legacy implementation, so tcp_probe.py classifies
# errors directly from real socket exception types -- tested here at the
# probe level, where that classification actually happens. socket.socket
# is fully mocked throughout -- no real network access is used or needed.
# ---------------------------------------------------------------------------

class _FakeSocket:
    """Records settimeout()/close() calls; connect() either succeeds
    silently or raises a pre-configured exception."""

    def __init__(self, connect_raises: Exception | None = None):
        self._connect_raises = connect_raises
        self.timeout_set: float | None = None
        self.closed = False
        self.connected_to = None

    def settimeout(self, timeout):
        self.timeout_set = timeout

    def connect(self, address):
        self.connected_to = address
        if self._connect_raises is not None:
            raise self._connect_raises

    def close(self):
        self.closed = True


def test_tcp_adapter_satisfies_probe_protocol_and_reports_tcp_type():
    adapter = TCPProbeAdapter()
    assert isinstance(adapter, Probe)
    assert adapter.probe_type == ProbeType.TCP


def test_tcp_adapter_forwards_target_port_and_timeout_to_connect(monkeypatch):
    """Confirms the adapter is a pure pass-through, per its own
    docstring -- no classification logic of its own, just delegation."""
    captured = {}

    def fake_connect(host, **kwargs):
        captured["host"] = host
        captured["kwargs"] = kwargs
        return RawMeasurement(probe_type=ProbeType.TCP, target=f"{host}:{kwargs.get('port')}", success=True)

    monkeypatch.setattr(tcp_probe, "connect", fake_connect)

    result = TCPProbeAdapter().run("example.com", port=443, timeout=1.5)

    assert isinstance(result, RawMeasurement)
    assert captured["host"] == "example.com"
    assert captured["kwargs"] == {"port": 443, "timeout": 1.5}


def test_tcp_probe_successful_connection_returns_successful_measurement(monkeypatch):
    fake_socket = _FakeSocket()
    monkeypatch.setattr(tcp_probe.socket, "socket", lambda *a, **k: fake_socket)

    result = tcp_probe.connect("192.0.2.1", 443)

    assert result.success is True
    assert result.probe_type == ProbeType.TCP
    assert result.target == "192.0.2.1:443"
    assert result.error is None
    assert result.error_type is None
    assert fake_socket.connected_to == ("192.0.2.1", 443)


def test_tcp_probe_uses_monotonic_clock_for_latency(monkeypatch):
    """Confirms time.perf_counter() (monotonic) is what drives
    latency_ms, not datetime -- per the task's explicit requirement."""
    fake_socket = _FakeSocket()
    monkeypatch.setattr(tcp_probe.socket, "socket", lambda *a, **k: fake_socket)

    counter_values = iter([100.0, 100.25])  # 250ms elapsed
    monkeypatch.setattr(tcp_probe.time, "perf_counter", lambda: next(counter_values))

    result = tcp_probe.connect("192.0.2.1", 443)

    assert result.latency_ms == 250.0


def test_tcp_probe_passes_configured_timeout_to_socket(monkeypatch):
    fake_socket = _FakeSocket()
    monkeypatch.setattr(tcp_probe.socket, "socket", lambda *a, **k: fake_socket)

    tcp_probe.connect("192.0.2.1", 443, timeout=3.5)

    assert fake_socket.timeout_set == 3.5


def test_tcp_probe_classifies_each_socket_exception_type_correctly(monkeypatch):
    """Covers 'connection refused/error is handled correctly' and
    'invalid/unavailable target behavior is handled deterministically'
    for every exception branch tcp_probe.connect() implements, using
    the real exception types (never string matching)."""
    cases = [
        (tcp_probe.socket.timeout(), ProbeErrorType.TIMEOUT),
        (ConnectionRefusedError("Connection refused"), ProbeErrorType.CONNECTION_REFUSED),
        (PermissionError("Operation not permitted"), ProbeErrorType.PERMISSION_DENIED),
        (OSError("Network is unreachable"), ProbeErrorType.UNKNOWN),
    ]
    for exc, expected_type in cases:
        fake_socket = _FakeSocket(connect_raises=exc)
        monkeypatch.setattr(tcp_probe.socket, "socket", lambda *a, **k: fake_socket)

        result = tcp_probe.connect("192.0.2.1", 443)

        assert result.success is False, expected_type
        assert result.error_type == expected_type, expected_type
        assert result.error is not None


def test_tcp_probe_closes_socket_after_successful_attempt(monkeypatch):
    fake_socket = _FakeSocket()
    monkeypatch.setattr(tcp_probe.socket, "socket", lambda *a, **k: fake_socket)

    tcp_probe.connect("192.0.2.1", 443)

    assert fake_socket.closed is True


def test_tcp_probe_closes_socket_after_failed_attempt(monkeypatch):
    fake_socket = _FakeSocket(connect_raises=ConnectionRefusedError("refused"))
    monkeypatch.setattr(tcp_probe.socket, "socket", lambda *a, **k: fake_socket)

    tcp_probe.connect("192.0.2.1", 443)

    assert fake_socket.closed is True
