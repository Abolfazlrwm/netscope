"""
Adapter making the existing netscope.probes.dns_probe.resolve() function
satisfy netscope.core.ports.Probe.

Contains no measurement logic of its own -- dns_probe.py is not modified
and is not reimplemented here. This class only translates the Probe
Protocol's run(target, **options) call shape into a call to the existing
resolve(hostname, record_type, resolver_ip, timeout) function (target
maps to hostname, the function's first positional parameter), and
returns whatever it returns, unchanged except for one addition
(TASK-015, see below).

TASK-015 addition: same migration pattern as TASK-014 (ICMP), applied
to DNS. Classifies dns_probe.resolve()'s existing free-text `error`
string into a structured ProbeErrorType, attached to the returned
RawMeasurement's `error_type` field. dns_probe.py's own measurement
logic -- its try/except around dns.resolver.Resolver.resolve() -- is
completely unmodified; classification happens entirely here, working
only from what dns_probe.py already returns.
"""

from __future__ import annotations

from typing import Any

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement
from netscope.probes import dns_probe


def _classify_dns_error(measurement: RawMeasurement) -> ProbeErrorType | None:
    """Best-effort classification of a DNS RawMeasurement's existing
    free-text `error` string into a ProbeErrorType. Returns None for a
    successful measurement -- there is nothing to classify.

    dns_probe.py's resolve() catches any exception from
    dns.resolver.Resolver.resolve() with a blanket `except Exception`
    and stores str(exc) as `error` (unmodified by this task -- see that
    file), so classification here works from that string, the same way
    the ICMP adapter's _classify_icmp_error() works from icmp_probe.py's
    strings:
    - "not installed" -> PROBE_UNAVAILABLE (mirrors ICMP's case for a
      missing dnspython installation).
    - "timed out"/"timeout" -> TIMEOUT (dns.exception.Timeout's own
      string representation is "The DNS operation timed out.").
    - any other failure with a message -- NXDOMAIN, NoAnswer,
      NoNameservers, and anything else dnspython can raise -> DNS_FAILURE,
      a DNS-specific bucket distinct from the fully generic UNKNOWN, per
      architecture-overview.md SS6's anticipated taxonomy.
    - success=False with no error string at all -- unlike ICMP,
      dns_probe.py's structure always attaches an error string on
      failure (there is no "ran cleanly but no response" path the way
      icmplib.ping() has), so this is a defensive case that should not
      occur in practice -> UNKNOWN, rather than guessing DNS_FAILURE or
      TIMEOUT for a case with no evidence either way.
    """
    if measurement.success:
        return None

    if measurement.error is None:
        return ProbeErrorType.UNKNOWN

    error_text = measurement.error.lower()
    if "not installed" in error_text:
        return ProbeErrorType.PROBE_UNAVAILABLE
    if "timed out" in error_text or "timeout" in error_text:
        return ProbeErrorType.TIMEOUT
    return ProbeErrorType.DNS_FAILURE


class DNSProbeAdapter:
    """Satisfies netscope.core.ports.Probe by wrapping dns_probe.resolve()."""

    probe_type = ProbeType.DNS

    def run(self, target: str, **options: Any) -> RawMeasurement:
        measurement = dns_probe.resolve(target, **options)
        measurement.error_type = _classify_dns_error(measurement)
        return measurement
