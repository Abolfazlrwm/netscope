"""
Tests for netscope.app.container (TASK-035 additions).

Fully offline: configure_logging only touches the stdlib logging
module's in-process configuration, never real I/O. Container
construction tests never call build_container() (which would open a
real database file) -- they construct Container directly, exactly as
tests/test_probe_registry.py and tests/test_use_cases.py already do.
"""

from __future__ import annotations

import logging

from netscope.adapters.probes.registry import ProbeRegistry
from netscope.app.container import Container, _NETSCOPE_PACKAGES, configure_logging
from netscope.core.models import ProbeType
from netscope.persistence.measurement_repository import MeasurementRepository


# ---------------------------------------------------------------------------
# measurement_repository defaults to None -- no side effects from bare
# construction
# ---------------------------------------------------------------------------


def test_container_measurement_repository_defaults_to_none():
    container = Container(probe_registry=ProbeRegistry(probes={}))
    assert container.measurement_repository is None


def test_container_without_measurement_repository_does_not_touch_the_filesystem(tmp_path, monkeypatch):
    """Regression guard for the exact risk this task's own container.py
    docstring names: constructing a bare Container must never create
    ~/.netscope or any other file, unlike build_container()."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    Container(probe_registry=ProbeRegistry(probes={}))
    assert not (tmp_path / ".netscope").exists()


def test_container_accepts_an_explicit_measurement_repository():
    repo = MeasurementRepository.open(":memory:")
    container = Container(probe_registry=ProbeRegistry(probes={}), measurement_repository=repo)
    assert container.measurement_repository is repo
    repo.close()


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


def test_configure_logging_defaults_to_warning():
    configure_logging(verbose=False)
    for package in _NETSCOPE_PACKAGES:
        assert logging.getLogger(package).level == logging.WARNING


def test_configure_logging_verbose_raises_to_info():
    configure_logging(verbose=True)
    for package in _NETSCOPE_PACKAGES:
        assert logging.getLogger(package).level == logging.INFO
    configure_logging(verbose=False)  # reset for any other test relying on the default


def test_configure_logging_sets_every_top_level_package_logger():
    """Per architecture-decisions.md's Logging decision: one logger per
    top-level package, not just the root logger."""
    assert set(_NETSCOPE_PACKAGES) == {"netscope.core", "netscope.adapters", "netscope.persistence", "netscope.app", "netscope.ui"}
    configure_logging(verbose=True)
    for package in _NETSCOPE_PACKAGES:
        assert logging.getLogger(package).level == logging.INFO
    configure_logging(verbose=False)
