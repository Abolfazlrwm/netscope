"""
Tests for netscope.core.ports.

These verify CONTRACT shape only: that a conforming fake satisfies the
Probe Protocol structurally, that a non-conforming object does not, and
that the contract reuses existing domain models rather than introducing
new ones. No real ICMP/DNS/HTTP/socket/traceroute/internet access is
used or needed -- everything here is fully offline and deterministic,
consistent with the rest of the test suite (tests/test_baseline.py,
test_experience_score.py, test_diagnosis.py, test_models.py,
test_explanation.py).
"""

from __future__ import annotations

import pytest

from netscope.core.baseline import UserBaseline
from netscope.core.models import ProbeType, RawMeasurement
from netscope.core.ports import BaselineRepository, Probe


class _FakeICMPProbe:
    """A minimal, in-memory stand-in for a real adapter. Deliberately does
    NOT inherit from Probe -- Protocol is structural, so satisfying the
    shape is enough. This mirrors how a real adapter (e.g. a future
    ICMPEchoProbe wrapping icmplib) is expected to relate to this contract:
    no inheritance needed, per ports.py's own docstring."""

    probe_type = ProbeType.ICMP

    def run(self, target: str, **options: object) -> RawMeasurement:
        return RawMeasurement(
            probe_type=self.probe_type,
            target=target,
            success=True,
            latency_ms=1.0,
        )


class _FakeDNSProbe:
    """A second, differently-typed fake, to confirm the contract isn't
    accidentally tied to one probe type."""

    probe_type = ProbeType.DNS

    def run(self, target: str, **options: object) -> RawMeasurement:
        return RawMeasurement(probe_type=self.probe_type, target=target, success=False, error="nxdomain")


class _MissingRunMethod:
    """Has the right attribute but no run() -- must NOT satisfy Probe."""

    probe_type = ProbeType.HTTP


class _MissingProbeTypeAttribute:
    """Has a run() method but no probe_type -- must NOT satisfy Probe."""

    def run(self, target: str, **options: object) -> RawMeasurement:
        return RawMeasurement(probe_type=ProbeType.TCP, target=target)


class _NotAProbeAtAll:
    """Completely unrelated shape -- must NOT satisfy Probe."""

    def __init__(self) -> None:
        self.value = 42


def test_conforming_fake_satisfies_probe_protocol_structurally():
    """No inheritance from Probe is used here -- this is the point of a
    structural Protocol: any object with the right shape counts."""
    fake = _FakeICMPProbe()
    assert isinstance(fake, Probe)


def test_a_second_differently_typed_fake_also_satisfies_probe():
    fake = _FakeDNSProbe()
    assert isinstance(fake, Probe)


def test_object_missing_run_method_does_not_satisfy_probe():
    assert isinstance(_MissingRunMethod(), Probe) is False


def test_object_missing_probe_type_attribute_does_not_satisfy_probe():
    assert isinstance(_MissingProbeTypeAttribute(), Probe) is False


def test_unrelated_object_does_not_satisfy_probe():
    assert isinstance(_NotAProbeAtAll(), Probe) is False


def test_probe_run_returns_the_existing_raw_measurement_model_not_a_new_type():
    """Confirms the contract reuses core.models.RawMeasurement rather than
    introducing a duplicate result model, per the task's explicit
    requirement not to duplicate existing domain models."""
    fake = _FakeICMPProbe()
    result = fake.run("1.1.1.1")
    assert isinstance(result, RawMeasurement)
    assert result.probe_type == ProbeType.ICMP
    assert result.target == "1.1.1.1"


def test_probe_type_attribute_exposes_the_existing_probe_type_enum():
    """Confirms probe_type is the existing netscope.core.models.ProbeType
    enum, not a new duplicate classification scheme."""
    fake = _FakeDNSProbe()
    assert fake.probe_type is ProbeType.DNS
    assert isinstance(fake.probe_type, ProbeType)


# ---------------------------------------------------------------------------
# BaselineRepository (TASK-024)
# ---------------------------------------------------------------------------


class _FakeBaselineRepository:
    """A minimal, in-memory stand-in for a future persistence-layer
    implementation (e.g. SQLite-backed). Deliberately does NOT inherit
    from BaselineRepository -- Protocol is structural, mirroring how
    _FakeICMPProbe/_FakeDNSProbe relate to Probe above. No real
    database/file access is used -- fully offline and deterministic."""

    def __init__(self) -> None:
        self._stored: UserBaseline | None = None

    def save(self, baseline: UserBaseline) -> None:
        self._stored = baseline

    def load(self) -> UserBaseline:
        if self._stored is None:
            return UserBaseline()
        return self._stored


class _MissingSaveMethod:
    """Has load() but no save() -- must NOT satisfy BaselineRepository."""

    def load(self) -> UserBaseline:
        return UserBaseline()


class _MissingLoadMethod:
    """Has save() but no load() -- must NOT satisfy BaselineRepository."""

    def save(self, baseline: UserBaseline) -> None:
        pass


def test_conforming_fake_satisfies_baseline_repository_protocol_structurally():
    """No inheritance from BaselineRepository is used here -- same
    structural-typing point as the Probe tests above."""
    fake = _FakeBaselineRepository()
    assert isinstance(fake, BaselineRepository)


def test_object_missing_save_method_does_not_satisfy_baseline_repository():
    assert isinstance(_MissingSaveMethod(), BaselineRepository) is False


def test_object_missing_load_method_does_not_satisfy_baseline_repository():
    assert isinstance(_MissingLoadMethod(), BaselineRepository) is False


def test_unrelated_object_does_not_satisfy_baseline_repository():
    assert isinstance(_NotAProbeAtAll(), BaselineRepository) is False


def test_baseline_repository_round_trips_the_existing_user_baseline_model_not_a_new_type():
    """Confirms the contract reuses core.baseline.UserBaseline rather
    than introducing a duplicate persistence-facing model, mirroring the
    equivalent RawMeasurement guarantee for Probe above."""
    fake = _FakeBaselineRepository()
    ub = UserBaseline()
    ub.observe_latency("1.1.1.1", 20.0)

    fake.save(ub)
    loaded = fake.load()

    assert isinstance(loaded, UserBaseline)
    assert loaded is ub
    assert loaded.latency["1.1.1.1"].mean == pytest.approx(20.0)


def test_baseline_repository_load_with_nothing_saved_yet_returns_a_fresh_user_baseline():
    """A conforming repository is free to return an empty baseline when
    nothing has been persisted yet, rather than raising -- this is a
    property of the port shape (load() -> UserBaseline, no Optional),
    not a specific persistence implementation being tested here."""
    fake = _FakeBaselineRepository()
    loaded = fake.load()

    assert isinstance(loaded, UserBaseline)
    assert loaded.latency == {}
    assert loaded.packet_loss == {}


def test_ports_module_only_imports_stdlib_typing_and_core_models():
    """Enforces the design rule directly by inspecting core/ports.py's
    actual AST import statements (not a text search, which would false-
    positive on this module's own docstring -- it intentionally mentions
    icmplib/dnspython/httpx/psutil by name in prose, to explain why they
    are NOT imported). This is a regression guard: a future edit that
    accidentally adds a real import of one of these libraries fails this
    test immediately rather than being caught only by manual review."""
    import ast
    import netscope.core.ports as ports_module

    with open(ports_module.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module.split(".")[0])

    allowed = {"__future__", "typing", "netscope"}
    assert imported_modules <= allowed, (
        f"core/ports.py imports {imported_modules - allowed}, which are "
        "not in the allowed set for a dependency-light core contract"
    )
