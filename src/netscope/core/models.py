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


@dataclass
class NetworkInterface:
    """One local network interface, as reported by the OS.

    Deliberately minimal for now (TASK-010, "local interface discovery"
    only): name, up/down state, and its addresses. Gateway association,
    DNS servers, and connection-type classification (Wi-Fi/Ethernet/
    cellular) are separate, later concerns (future-roadmap.md TASK-011
    "Gateway discovery" and TASK-012 "Network type detection") and are
    intentionally not fields here yet -- adding them now would be
    speculative, ahead of the code that would populate them.
    """

    name: str
    is_up: bool
    addresses: list[str] = field(default_factory=list)
    is_loopback: bool = False


@dataclass
class NetworkSnapshot:
    """The set of local network interfaces at one point in time.

    Named to match the existing RouteSnapshot convention (a snapshot of
    something that can change between measurement rounds, e.g. a laptop
    switching from Wi-Fi to Ethernet) rather than the more speculative
    "NetworkContext" name used as a placeholder in earlier architecture
    documentation -- see adr-010-network-discovery.md for why this name
    was chosen instead.
    """

    timestamp: datetime = field(default_factory=utcnow)
    interfaces: list[NetworkInterface] = field(default_factory=list)
