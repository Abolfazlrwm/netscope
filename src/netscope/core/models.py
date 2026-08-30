"""
Core domain models for NetScope.

These are plain, dependency-free dataclasses. Every probe in netscope.probes
produces a RawMeasurement. Everything downstream (routing, intelligence,
diagnosis) consumes and enriches these models -- it never talks to a probe
library directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProbeType(str, Enum):
    ICMP = "icmp"
    DNS = "dns"
    TCP = "tcp"
    TLS = "tls"
    HTTP = "http"
    TRACEROUTE = "traceroute"


class ProbeErrorType(str, Enum):
    """Structured classification of why a probe failed, so downstream
    code can branch on failure category rather than parsing free-text
    error strings. See architecture-overview.md SS6 for the full future
    taxonomy this is a deliberately small subset of.

    Scope note: only the values ICMP (TASK-014), DNS (TASK-015), TCP
    (TASK-016), TLS (TASK-017), and HTTP (TASK-018) can currently
    produce are implemented here -- TIMEOUT, PERMISSION_DENIED,
    PROBE_UNAVAILABLE, DNS_FAILURE, CONNECTION_REFUSED, TLS_FAILURE,
    HTTP_FAILURE, UNKNOWN. This is NOT the complete taxonomy
    architecture-overview.md anticipates (which also lists
    NETWORK_UNREACHABLE, PLATFORM_UNSUPPORTED, etc.). Those remain
    unimplemented until the probes/adapters that can actually produce
    them are built in their own, separately-scoped future tasks --
    adding unused values now would be speculative.
    """

    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    PROBE_UNAVAILABLE = "probe_unavailable"
    DNS_FAILURE = "dns_failure"
    CONNECTION_REFUSED = "connection_refused"
    TLS_FAILURE = "tls_failure"
    HTTP_FAILURE = "http_failure"
    UNKNOWN = "unknown"


@dataclass
class RawMeasurement:
    """A single, raw measurement from one probe. No interpretation here."""

    probe_type: ProbeType
    target: str
    timestamp: datetime = field(default_factory=utcnow)

    success: bool = False
    latency_ms: Optional[float] = None
    packet_loss_pct: Optional[float] = None
    jitter_ms: Optional[float] = None
    error: Optional[str] = None
    error_type: Optional[ProbeErrorType] = None

    # Free-form extra data specific to the probe type
    # (e.g. resolved IP for DNS, status_code for HTTP, hop list for traceroute)
    extra: dict = field(default_factory=dict)


@dataclass
class RouteHop:
    ttl: int
    address: Optional[str]
    hostname: Optional[str]
    avg_rtt_ms: Optional[float]
    packet_loss_pct: float
    asn: Optional[str] = None
    is_unstable: bool = False


@dataclass
class RouteSnapshot:
    """A traceroute-style path to a target, captured at one point in time."""

    target: str
    timestamp: datetime = field(default_factory=utcnow)
    hops: list[RouteHop] = field(default_factory=list)

    def signature(self) -> str:
        """Cheap fingerprint used to detect route changes (route churn)."""
        return "|".join(h.address or "*" for h in self.hops)


class ExperienceLevel(str, Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    DEGRADED = "degraded"
    POOR = "poor"
    DOWN = "down"


@dataclass
class ExperienceEvent:
    """The output of intelligence.experience_score: a single scored moment."""

    timestamp: datetime
    score: float  # 0-100
    level: ExperienceLevel
    contributing_measurements: list[RawMeasurement] = field(default_factory=list)


@dataclass
class Incident:
    """A sustained deviation from the user's personal baseline."""

    started_at: datetime
    ended_at: Optional[datetime] = None
    signals: list[str] = field(default_factory=list)  # e.g. ["latency", "loss", "route_change"]
    likely_cause: Optional[str] = None
    confidence_pct: Optional[float] = None
    evidence: list[str] = field(default_factory=list)
    explanation: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


class NetworkType(str, Enum):
    """Best-effort classification of a network interface's connection
    type. See adapters/discovery/network_type_classifier.py for how
    this is derived -- classification is inherently best-effort (based
    on interface naming conventions, which vary by OS and driver), so
    UNKNOWN is a legitimate, expected outcome, not an error case."""

    WIFI = "wifi"
    ETHERNET = "ethernet"
    CELLULAR = "cellular"
    UNKNOWN = "unknown"


@dataclass
class NetworkInterface:
    """One local network interface, as reported by the OS.

    Gateway association and DNS servers remain separate, later concerns
    (future-roadmap.md TASK-011 "Gateway discovery") and are
    intentionally not fields here yet -- adding them now would be
    speculative, ahead of the code that would populate them.

    network_type (TASK-012, "Network type detection") is a best-effort
    classification, not a guarantee -- see NetworkType's docstring.
    """

    name: str
    is_up: bool
    addresses: list[str] = field(default_factory=list)
    is_loopback: bool = False
    network_type: NetworkType = NetworkType.UNKNOWN


@dataclass
class NetworkSnapshot:
    """The set of local network interfaces at one point in time.

    Named to match the existing RouteSnapshot convention (a snapshot of
    something that can change between measurement rounds, e.g. a laptop
    switching from Wi-Fi to Ethernet) -- see adr-010-network-discovery.md
    for why this name was chosen over an earlier placeholder name.

    Note: an earlier version of this docstring used "NetworkContext" as
    a discarded placeholder name for *this* type. TASK-013 later
    introduced NetworkContext as a distinct, separate type (below) that
    wraps a NetworkSnapshot rather than replacing it -- the two are not
    the same concept.
    """

    timestamp: datetime = field(default_factory=utcnow)
    interfaces: list[NetworkInterface] = field(default_factory=list)


@dataclass
class NetworkContext:
    """Assembled result of network discovery (TASK-013, "Network
    metadata") -- the single domain object application-level code is
    expected to consume, rather than reaching into a NetworkSnapshot
    directly at every call site.

    Deliberately thin: it wraps an existing NetworkSnapshot rather than
    duplicating its fields. NetworkSnapshot keeps its own, narrower
    meaning ("just the interfaces at one point in time" -- see its own
    docstring above); NetworkContext is the assembly point where future
    discovery metadata not yet implemented (e.g. default gateway, DNS
    servers -- future-roadmap.md TASK-011 "Gateway discovery") is
    expected to attach directly to this type, without requiring another
    wrapper to be introduced later.

    Intentionally has no derived/heuristic properties (e.g. a "primary
    interface" guess) and no business logic -- TASK-013's scope is
    assembly only, not interpretation, which remains core.diagnosis's
    responsibility per architecture-overview.md's layering.
    """

    snapshot: NetworkSnapshot

    @classmethod
    def from_snapshot(cls, snapshot: NetworkSnapshot) -> NetworkContext:
        """Pure, dependency-free assembly: wraps an already-obtained
        NetworkSnapshot into a NetworkContext. Adapters call this (see
        adapters/discovery/network_discovery.py's discover_context())
        rather than every call site constructing NetworkContext by hand."""
        return cls(snapshot=snapshot)
