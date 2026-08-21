"""
Characterization tests for netscope.diagnosis.engine.

This module currently uses fixed static thresholds and does NOT consult
intelligence/baseline.py (see implementation-audit.md, REWRITE decision).
These tests document CURRENT behavior only -- several of these are known
to encode a bug (see the "known bad behavior" tests below, named and
commented accordingly) and must NOT be read as a desired-behavior contract.
"""

from __future__ import annotations

import pytest

from netscope.core.models import ProbeType, RawMeasurement
from netscope.diagnosis.engine import diagnose


def _healthy(target="x"):
    return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=True, latency_ms=10.0, packet_loss_pct=0.0)


def _high_latency(target="x", latency_ms=300.0):
    return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=True, latency_ms=latency_ms, packet_loss_pct=0.0)


def _high_loss(target="x", loss_pct=10.0):
    return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=True, latency_ms=10.0, packet_loss_pct=loss_pct)


def _unreachable(target="x"):
    return RawMeasurement(probe_type=ProbeType.ICMP, target=target, success=False, error="timeout")


# ---------------------------------------------------------------------------
# Healthy / no-issue case
# ---------------------------------------------------------------------------

def test_diagnosis_no_issue_when_all_reference_points_are_healthy():
    d = diagnose(local_gateway=_healthy(), public_dns=_healthy(), public_cdn=_healthy())
    assert d.likely_cause == "No issue detected"
    assert d.confidence_pct == pytest.approx(90.0)
    assert d.ruled_out == []


# ---------------------------------------------------------------------------
# Local gateway bad -> local network issue, everything else ruled out
# ---------------------------------------------------------------------------

def test_diagnosis_bad_gateway_latency_is_diagnosed_as_local_issue():
    d = diagnose(local_gateway=_high_latency(), public_dns=_healthy(), public_cdn=_healthy())
    assert "Local network issue" in d.likely_cause
    assert d.confidence_pct == pytest.approx(80.0)


def test_diagnosis_unreachable_gateway_is_diagnosed_as_local_issue():
    d = diagnose(local_gateway=_unreachable(), public_dns=_healthy(), public_cdn=_healthy())
    assert "Local network issue" in d.likely_cause
    assert d.confidence_pct == pytest.approx(80.0)


def test_diagnosis_bad_gateway_rules_out_isp_and_destination_causes():
    d = diagnose(local_gateway=_high_latency(), public_dns=_healthy(), public_cdn=_healthy())
    assert any("ISP-side or destination-side" in r for r in d.ruled_out)


def test_diagnosis_bad_gateway_takes_priority_even_if_everything_else_is_also_bad():
    """Current implementation checks gateway first and short-circuits --
    it never reports an ISP or destination cause once the gateway itself
    looks bad, regardless of what the other two measurements show."""
    d = diagnose(local_gateway=_high_latency(), public_dns=_high_latency(), public_cdn=_high_latency())
    assert "Local network issue" in d.likely_cause


# ---------------------------------------------------------------------------
# Gateway healthy, DNS + CDN both bad -> upstream ISP issue
# ---------------------------------------------------------------------------

def test_diagnosis_high_packet_loss_upstream_is_diagnosed_as_isp_issue():
    d = diagnose(
        local_gateway=_healthy(),
        public_dns=_high_loss(),
        public_cdn=_high_loss(),
    )
    assert "Upstream ISP issue" in d.likely_cause
    assert d.confidence_pct == pytest.approx(70.0)


def test_diagnosis_isp_issue_rules_out_local_and_destination_specific():
    d = diagnose(local_gateway=_healthy(), public_dns=_high_latency(), public_cdn=_high_latency())
    assert any("Local network issue" in r for r in d.ruled_out)
    assert any("Destination-specific issue" in r for r in d.ruled_out)


# ---------------------------------------------------------------------------
# Gateway + DNS healthy, only CDN bad -> destination-specific
# ---------------------------------------------------------------------------

def test_diagnosis_destination_unreachable_is_diagnosed_as_destination_specific():
    d = diagnose(local_gateway=_healthy(), public_dns=_healthy(), public_cdn=_unreachable())
    assert "Destination-specific issue" in d.likely_cause
    assert d.confidence_pct == pytest.approx(65.0)


def test_diagnosis_destination_specific_rules_out_local_and_general_isp():
    d = diagnose(local_gateway=_healthy(), public_dns=_healthy(), public_cdn=_high_latency())
    assert any("Local network issue" in r for r in d.ruled_out)
    assert any("General ISP/upstream issue" in r for r in d.ruled_out)


# ---------------------------------------------------------------------------
# Missing measurements (None inputs)
# ---------------------------------------------------------------------------

def test_diagnosis_with_all_measurements_missing_reports_no_issue():
    """Current behavior when nothing was measured at all: since is_bad(None)
    returns False for every input, this falls through to the same
    'No issue detected' branch as three genuinely healthy measurements.
    This is part of the same missing-data gap covered below."""
    d = diagnose(local_gateway=None, public_dns=None, public_cdn=None)
    assert d.likely_cause == "No issue detected"


def test_untested_gateway_currently_behaves_as_healthy():
    """KNOWN BAD BEHAVIOR (see implementation-audit.md STEP 2/STEP 7):
    when local_gateway is None (never measured, e.g. --gateway not
    supplied), the engine's internal is_bad(None) returns False, so an
    *untested* gateway is treated identically to a *healthy* one. Here,
    DNS and CDN are both bad, so the engine reports an "Upstream ISP
    issue" and explicitly claims to have "ruled out" a local network
    issue -- despite the local network never having been measured.

    This test intentionally documents the CURRENT (buggy) behavior so
    that TASK-010 (wire baseline into diagnosis / fix this) has a
    regression test to update, not a bug to silently re-discover.
    This is NOT a desired-behavior contract.
    """
    d = diagnose(local_gateway=None, public_dns=_high_loss(), public_cdn=_high_loss())

    assert "Upstream ISP issue" in d.likely_cause
    assert any("Local network issue" in r for r in d.ruled_out)


def test_diagnosis_missing_dns_and_cdn_with_healthy_gateway_reports_no_issue():
    """Symmetric case: gateway healthy, dns/cdn both None (untested) ->
    both treated as 'not bad', so overall falls through to 'No issue
    detected'. Same underlying gap as the gateway case above, just
    applied to the other two inputs."""
    d = diagnose(local_gateway=_healthy(), public_dns=None, public_cdn=None)
    assert d.likely_cause == "No issue detected"


# ---------------------------------------------------------------------------
# Threshold boundaries (exact values from the current implementation)
# ---------------------------------------------------------------------------

def test_diagnosis_latency_exactly_at_threshold_is_not_bad():
    """Current implementation uses a strict '>' comparison: latency_ms
    of exactly 250 is NOT considered bad."""
    boundary = RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=True, latency_ms=250.0, packet_loss_pct=0.0)
    d = diagnose(local_gateway=boundary, public_dns=_healthy(), public_cdn=_healthy())
    assert d.likely_cause == "No issue detected"


def test_diagnosis_latency_just_above_threshold_is_bad():
    boundary = RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=True, latency_ms=250.01, packet_loss_pct=0.0)
    d = diagnose(local_gateway=boundary, public_dns=_healthy(), public_cdn=_healthy())
    assert "Local network issue" in d.likely_cause


def test_diagnosis_packet_loss_exactly_at_threshold_is_not_bad():
    """Current implementation: packet_loss_pct of exactly 5.0 is NOT
    considered bad (strict '>' comparison)."""
    boundary = RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=True, latency_ms=10.0, packet_loss_pct=5.0)
    d = diagnose(local_gateway=boundary, public_dns=_healthy(), public_cdn=_healthy())
    assert d.likely_cause == "No issue detected"


def test_diagnosis_packet_loss_just_above_threshold_is_bad():
    boundary = RawMeasurement(probe_type=ProbeType.ICMP, target="x", success=True, latency_ms=10.0, packet_loss_pct=5.01)
    d = diagnose(local_gateway=boundary, public_dns=_healthy(), public_cdn=_healthy())
    assert "Local network issue" in d.likely_cause
