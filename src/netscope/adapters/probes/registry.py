"""
netscope.adapters.probes.registry

Maps ProbeType -> a concrete Probe-conforming adapter instance, so that
higher layers (the future app use cases, and eventually ui) can ask for
"the ICMP probe" without importing or knowing about ICMPProbeAdapter (or
any other concrete adapter class) directly.

This is deliberately small: a dict lookup with a clearly-defined error
for an unregistered probe type, and a register() method for extending
it. No plugin system, no auto-discovery, no dependency-injection
framework -- per the task's explicit instruction not to build a
framework, and per architecture-overview.md's repeated principle of
avoiding unnecessary abstraction.
"""

from __future__ import annotations

from netscope.adapters.probes.dns_adapter import DNSProbeAdapter
from netscope.adapters.probes.http_adapter import HTTPProbeAdapter
from netscope.adapters.probes.icmp_adapter import ICMPProbeAdapter
from netscope.adapters.probes.tcp_adapter import TCPProbeAdapter
from netscope.adapters.probes.tls_adapter import TLSProbeAdapter
from netscope.core.models import ProbeType
from netscope.core.ports import Probe


class ProbeNotRegisteredError(LookupError):
    """Raised when ProbeRegistry.get() is asked for a ProbeType that has
    no registered adapter. A defined, explicit failure mode -- callers
    are never handed None or a silently-wrong probe."""


class ProbeRegistry:
    """Looks up a Probe-conforming adapter by ProbeType.

    Registered by default: ICMP, DNS, HTTP, TCP, TLS -- the five
    adapters that exist today (netscope.adapters.probes.icmp_adapter/
    dns_adapter/http_adapter, added in TASK-007; tcp_adapter, added in
    TASK-016; tls_adapter, added in TASK-017). TRACEROUTE is a valid
    ProbeType member but has no adapter implementation yet (per
    future-roadmap.md TASK-019) -- looking it up raises
    ProbeNotRegisteredError rather than returning None or an incorrect
    probe, so the failure is loud and immediate rather than a confusing
    AttributeError three calls later.
    """

    def __init__(self, probes: dict[ProbeType, Probe] | None = None) -> None:
        self._probes: dict[ProbeType, Probe] = (
            dict(probes) if probes is not None else self._default_probes()
        )

    @staticmethod
    def _default_probes() -> dict[ProbeType, Probe]:
        return {
            ProbeType.ICMP: ICMPProbeAdapter(),
            ProbeType.DNS: DNSProbeAdapter(),
            ProbeType.HTTP: HTTPProbeAdapter(),
            ProbeType.TCP: TCPProbeAdapter(),
            ProbeType.TLS: TLSProbeAdapter(),
        }

    def get(self, probe_type: ProbeType) -> Probe:
        """Return the registered Probe for `probe_type`.

        Raises ProbeNotRegisteredError if none is registered -- this is
        the "unknown probe type" behavior the task asks to have defined:
        explicit and immediate, not a silent None or a generic KeyError
        with no context.
        """
        try:
            return self._probes[probe_type]
        except KeyError as exc:
            registered = ", ".join(sorted(t.value for t in self._probes)) or "(none)"
            raise ProbeNotRegisteredError(
                f"No probe adapter registered for {probe_type!r}. "
                f"Registered probe types: {registered}."
            ) from exc

    def register(self, probe_type: ProbeType, probe: Probe) -> None:
        """Register (or replace) the adapter used for `probe_type`.

        Exists so a future probe (e.g. a traceroute adapter, TASK-019)
        or a test fake can be added/swapped without changing this class
        -- extending the registry never requires editing ProbeRegistry
        itself, only calling register() on an instance.
        """
        self._probes[probe_type] = probe

    def available_types(self) -> frozenset[ProbeType]:
        """The set of ProbeTypes currently registered."""
        return frozenset(self._probes)
