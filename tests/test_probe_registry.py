"""
Tests for netscope.adapters.probes.registry.ProbeRegistry and
netscope.app.container.Container/build_container.

Fully offline and deterministic -- no real ICMP/DNS/HTTP/network access.
Adapter behavior itself (delegation, error passthrough) is already
covered by tests/test_probe_adapters.py; these tests are about the
registry/container wiring layer only.
"""

from __future__ import annotations

from netscope.adapters.probes.dns_adapter import DNSProbeAdapter
from netscope.adapters.probes.http_adapter import HTTPProbeAdapter
from netscope.adapters.probes.icmp_adapter import ICMPProbeAdapter
from netscope.adapters.probes.registry import ProbeNotRegisteredError, ProbeRegistry
from netscope.app.container import Container, build_container
from netscope.core.models import ProbeType, RawMeasurement
from netscope.core.ports import Probe


# ---------------------------------------------------------------------------
# Registry returns the correct adapter
# ---------------------------------------------------------------------------

def test_registry_returns_icmp_adapter_for_icmp_probe_type():
    registry = ProbeRegistry()
    probe = registry.get(ProbeType.ICMP)
    assert isinstance(probe, ICMPProbeAdapter)
    assert probe.probe_type == ProbeType.ICMP


def test_registry_returns_dns_adapter_for_dns_probe_type():
    registry = ProbeRegistry()
    probe = registry.get(ProbeType.DNS)
    assert isinstance(probe, DNSProbeAdapter)
    assert probe.probe_type == ProbeType.DNS


def test_registry_returns_http_adapter_for_http_probe_type():
    registry = ProbeRegistry()
    probe = registry.get(ProbeType.HTTP)
    assert isinstance(probe, HTTPProbeAdapter)
    assert probe.probe_type == ProbeType.HTTP


# ---------------------------------------------------------------------------
# All registered adapters satisfy the Probe protocol
# ---------------------------------------------------------------------------

def test_all_default_registered_probes_satisfy_probe_protocol():
    registry = ProbeRegistry()
    for probe_type in registry.available_types():
        probe = registry.get(probe_type)
        assert isinstance(probe, Probe), f"{probe_type} adapter does not satisfy Probe"


def test_available_types_reports_exactly_the_three_implemented_probes():
    registry = ProbeRegistry()
    assert registry.available_types() == frozenset({ProbeType.ICMP, ProbeType.DNS, ProbeType.HTTP})


# ---------------------------------------------------------------------------
# Unknown probe type behavior is defined
# ---------------------------------------------------------------------------

def test_unregistered_probe_type_raises_probe_not_registered_error():
    registry = ProbeRegistry()
    # TCP has no adapter yet (future-roadmap.md TASK-016) -- looking it
    # up must fail loudly and explicitly, not return None or the wrong probe.
    try:
        registry.get(ProbeType.TCP)
        assert False, "expected ProbeNotRegisteredError to be raised"
    except ProbeNotRegisteredError as exc:
        assert "tcp" in str(exc).lower()
        assert "icmp" in str(exc).lower()  # message names what IS registered


def test_unregistered_probe_type_error_is_a_lookup_error():
    """ProbeNotRegisteredError is a LookupError subclass -- callers that
    already handle KeyError-family lookup failures generically still
    catch this correctly."""
    assert issubclass(ProbeNotRegisteredError, LookupError)


def test_empty_registry_raises_for_every_probe_type():
    registry = ProbeRegistry(probes={})
    for probe_type in ProbeType:
        try:
            registry.get(probe_type)
            assert False, f"expected ProbeNotRegisteredError for {probe_type}"
        except ProbeNotRegisteredError:
            pass


# ---------------------------------------------------------------------------
# Registry is extensible without modifying the class
# ---------------------------------------------------------------------------

def test_register_adds_a_new_probe_type_without_modifying_the_class():
    class _FakeTCPProbe:
        probe_type = ProbeType.TCP

        def run(self, target: str, **options: object) -> RawMeasurement:
            return RawMeasurement(probe_type=ProbeType.TCP, target=target, success=True)

    registry = ProbeRegistry()
    assert ProbeType.TCP not in registry.available_types()

    registry.register(ProbeType.TCP, _FakeTCPProbe())

    assert ProbeType.TCP in registry.available_types()
    probe = registry.get(ProbeType.TCP)
    assert isinstance(probe, Probe)
    assert probe.run("example.com").success is True


def test_register_can_replace_an_existing_probe_type():
    """Useful for tests: swap the real ICMP adapter for a fake without
    touching ProbeRegistry itself."""
    class _FakeICMPProbe:
        probe_type = ProbeType.ICMP

        def run(self, target: str, **options: object) -> RawMeasurement:
            return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=True, latency_ms=0.1)

    registry = ProbeRegistry()
    fake = _FakeICMPProbe()
    registry.register(ProbeType.ICMP, fake)

    assert registry.get(ProbeType.ICMP) is fake


def test_registry_constructor_accepts_a_custom_probe_mapping():
    class _FakeProbe:
        probe_type = ProbeType.HTTP

        def run(self, target: str, **options: object) -> RawMeasurement:
            return RawMeasurement(probe_type=ProbeType.HTTP, target=target, success=True)

    fake = _FakeProbe()
    registry = ProbeRegistry(probes={ProbeType.HTTP: fake})

    assert registry.available_types() == frozenset({ProbeType.HTTP})
    assert registry.get(ProbeType.HTTP) is fake


# ---------------------------------------------------------------------------
# Registry does not import low-level libraries
# ---------------------------------------------------------------------------

def test_registry_module_does_not_import_network_libraries_directly():
    """The registry only knows about adapters and core -- it must not
    import icmplib/dns/httpx/psutil directly, which would indicate it's
    doing measurement work itself rather than just routing to adapters."""
    import ast

    import netscope.adapters.probes.registry as registry_module

    with open(registry_module.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module.split(".")[0])

    forbidden = {"icmplib", "dns", "httpx", "psutil"}
    assert not (imported_modules & forbidden), (
        f"registry.py imports {imported_modules & forbidden} directly -- "
        "it must only route to adapters, never perform measurement itself"
    )
    # Sanity: it should import netscope (core + adapters), confirming
    # the AST walk itself is actually finding real imports.
    assert "netscope" in imported_modules


# ---------------------------------------------------------------------------
# Container creates a working registry
# ---------------------------------------------------------------------------

def test_build_container_returns_a_container_with_a_probe_registry():
    container = build_container()
    assert isinstance(container, Container)
    assert isinstance(container.probe_registry, ProbeRegistry)


def test_build_container_registry_has_all_three_default_probes_working():
    container = build_container()
    for probe_type in (ProbeType.ICMP, ProbeType.DNS, ProbeType.HTTP):
        probe = container.probe_registry.get(probe_type)
        assert isinstance(probe, Probe)
        assert probe.probe_type == probe_type


def test_container_accepts_an_explicit_registry_for_testing():
    custom_registry = ProbeRegistry(probes={})
    container = Container(probe_registry=custom_registry)
    assert container.probe_registry is custom_registry
