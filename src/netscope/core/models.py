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
    """One point in a traceroute (TASK-019's traceroute_probe.py
    produces these).

    asn/organization/country are lookup-ready placeholders (TASK-020,
    "Hop model" -- finalizing these fields per architecture-overview.md
    SS5): all three default to None because nothing populates them yet.
    They are intentionally NOT filled in by the traceroute probe itself
    -- per architecture-overview.md SS5 and ADR-003, ASN/organization/
    country come from a separate lookup adapter
    (future-roadmap.md TASK-022 "ASN/ISP intelligence",
    TASK-023 "Distance estimation"), not from icmplib.traceroute()'s
    own output, which has no concept of any of the three. Populating
    them is explicitly out of this task's scope.
    """

    ttl: int
    address: Optional[str]
    hostname: Optional[str]
    avg_rtt_ms: Optional[float]
    packet_loss_pct: float
    asn: Optional[str] = None
    organization: Optional[str] = None
    country: Optional[str] = None
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


class Severity(str, Enum):
    """How severe a single piece of Evidence -- or an Incident it
    contributed to -- is. Deliberately small and unopinionated: this
    model only defines the shared vocabulary. Deciding *which* severity
    a given observation deserves is diagnostic logic and belongs to
    core/diagnosis.py (TASK-026), not here.

    TASK-009 note: architecture-overview.md SS5 requires both Evidence
    and Incident to carry a `severity` without specifying concrete
    values, so this is this task's own minimal, explainable choice --
    not read from an existing convention elsewhere in the codebase (no
    prior Severity concept existed; ExperienceLevel is a different,
    unrelated 5-bucket scale for "how good is my overall experience
    right now" and is not reused here for "how severe is this one
    signal", a different question).
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Hypothesis(str, Enum):
    """The classification a Diagnosis can settle on. Verbatim from the
    enum architecture-overview.md SS11 specifies for the future
    evidence/hypothesis-based diagnosis engine (TASK-026).

    TASK-009 note: this task adds the enum as a canonical domain type
    only, per its own scope (core/models.py). Nothing yet classifies
    evidence into one of these values -- that selection logic is
    core/diagnosis.py's job (TASK-026), not this task's.

    INSUFFICIENT_EVIDENCE exists specifically so "not tested" -- or any
    case where available Evidence can't confidently support a more
    specific hypothesis -- has somewhere explicit to go, instead of
    silently defaulting to "no issue detected" the way an all-None
    input does in today's diagnosis/engine.py (the untested-gateway
    bug TASK-026 fixes).
    """

    LOCAL_NETWORK_ISSUE = "local_network_issue"
    ISP_ACCESS_ISSUE = "isp_access_issue"
    DNS_ISSUE = "dns_issue"
    ROUTING_DEGRADATION = "routing_degradation"
    DESTINATION_ISSUE = "destination_issue"
    SERVICE_ISSUE = "service_issue"
    GENERAL_CONNECTIVITY_ISSUE = "general_connectivity_issue"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass
class Evidence:
    """One structured signal a Diagnosis is built from -- replaces the
    free-form `list[str]` the audit flagged (architecture-overview.md
    SS5: "Must not be free-form text"; today's `diagnosis.engine.Diagnosis
    .evidence: list[str]` and `core.models.Incident.evidence: list[str]`
    are exactly that problem).

    `tested` is this model's answer to the headline audit bug: whether
    `metric` was actually measured at all. When `tested=False`,
    `observed_value`/`expected_value`/`deviation`/`source` are all None
    by construction (there is nothing to report -- fabricating a value
    for something never measured is exactly what must not happen) and
    this Evidence exists purely to record that absence as first-class
    information a future Diagnosis can act on (e.g. lean toward
    INSUFFICIENT_EVIDENCE), rather than the two states "tested and
    failed" / "tested and healthy" being the only options an absent
    Measurement could be squeezed into.

    Fields otherwise follow architecture-overview.md SS5 verbatim:
    `metric` (what was measured, e.g. "gateway_latency"),
    `observed_value`, `expected_value` (from baseline, if available),
    `deviation` (e.g. sigma, or None if no baseline yet), `severity`,
    `source` (the RawMeasurement or RouteSnapshot it came from), and
    `confidence` (this item's own confidence contribution, 0.0-1.0 --
    the same scale core/scoring.py's MetricEvaluation.confidence
    already uses for the analogous "how much should this count"
    question).
    """

    metric: str
    tested: bool = True
    observed_value: Optional[float] = None
    expected_value: Optional[float] = None
    deviation: Optional[float] = None
    severity: Severity = Severity.INFO
    source: Optional[RawMeasurement | RouteSnapshot] = None
    confidence: float = 1.0


@dataclass
class Diagnosis:
    """The canonical "a diagnosed problem" model (architecture-overview.md
    SS5/SS11). This is the single model TASK-009 introduces to resolve
    the audit's headline structural finding (architecture-overview.md
    SS1): `diagnosis/engine.py`'s own `Diagnosis` dataclass and
    `core.models.Incident`'s diagnosis-shaped fields "ended up as two
    competing models for the same concept."

    `classification`: one Hypothesis -- replaces today's free-text
    `likely_cause: str`.

    `evidence`: the full structured Evidence list this classification
    was built from -- replaces today's `evidence: list[str]`.

    `ruled_out`: what was considered and excluded. Typed as
    `list[Evidence | Hypothesis]` per architecture-overview.md SS5
    ("ruled_out: list[Evidence-or-Hypothesis]") because a thing can be
    ruled out two different ways -- a specific contradicting Evidence
    item, or an entire Hypothesis excluded outright with no single
    Evidence item pinned to it.

    `confidence`: replaces today's per-branch hand-picked constant
    (`80.0`/`70.0`/`65.0`/`90.0` in diagnosis/engine.py) -- SS11 says
    this must instead be "derived from the strength/count of supporting
    evidence". TASK-009 does not implement that derivation (no
    selection/classification logic belongs in core/models.py); it only
    gives the field somewhere correct to live for core/diagnosis.py
    (TASK-026) to populate.

    NOTE ON MIGRATION STATE: this canonical Diagnosis does not yet
    replace `diagnosis.engine.Diagnosis` in production -- `ui/cli.py`
    and `explanation/explainer.py` still import and use the old
    diagnosis.engine.Diagnosis shape (likely_cause/confidence_pct/
    evidence: list[str]/ruled_out: list[str]) and are deliberately left
    untouched by TASK-009's scope (core/models.py only; rewriting
    diagnosis/engine.py itself is explicitly TASK-026's job, which also
    fixes the untested-gateway bug this model's Evidence.tested field
    and Hypothesis.INSUFFICIENT_EVIDENCE value exist to support). Both
    Diagnosis shapes therefore coexist temporarily by design until
    TASK-026 migrates the real callers over.
    """

    classification: Hypothesis
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 0.0
    ruled_out: list[Evidence | Hypothesis] = field(default_factory=list)
    timestamp: datetime = field(default_factory=utcnow)


@dataclass
class Incident:
    """A sustained deviation from the user's personal baseline.

    TASK-009 reconciliation: architecture-overview.md SS5 says an
    Incident "Must represent: started_at, ended_at (...kept as-is),
    severity, the affected target/Service, evidence (the union of
    evidence across the incident's lifetime, not just its start), and
    the Diagnosis that explains it." The previous shape
    (`signals: list[str]`, `likely_cause: Optional[str]`,
    `confidence_pct: Optional[float]`, `evidence: list[str]`,
    `explanation: Optional[str]`) duplicated exactly the free-text
    diagnosis-shaped fields SS1 identifies as the audit's structural
    complaint -- those fields are removed here and replaced by a single
    `diagnosis: Diagnosis` reference plus a structured
    `evidence: list[Evidence]`. `signals: list[str]` is dropped
    entirely rather than kept alongside `evidence: list[Evidence]`:
    architecture-overview.md's documented field set for Incident does
    not include it, and `Evidence.metric` now serves the same
    "which signal" role structurally instead of as loose strings.

    `target` uses `str` (matching `RouteSnapshot.target`) rather than
    the `Service` type architecture-overview.md SS5 also mentions,
    because `Service` does not exist yet -- it is TASK-032's own
    later, separately-scoped task ("Service targets", future-roadmap.md
    Phase G). Introducing it here would be exactly the kind of
    speculative/early-future-task modeling TASK-009 is scoped against.

    `severity` and `diagnosis` default to None rather than a
    fabricated non-null value: an Incident's constructor should not
    have to assert a severity or attach a Diagnosis it wasn't actually
    given one for (this task adds the *shape*; core/incidents.py,
    TASK-028, is what actually produces populated Incidents from a
    sequence of Diagnoses).
    """

    started_at: datetime
    ended_at: Optional[datetime] = None
    severity: Optional[Severity] = None
    target: Optional[str] = None
    evidence: list[Evidence] = field(default_factory=list)
    diagnosis: Optional[Diagnosis] = None

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


# The five checks a Service can enable, per architecture-overview.md SS5
# ("a set of enabled checks (icmp, dns, tcp, tls, http, each optional)")
# and module-boundaries.md's Services section (same five, verbatim).
# Deliberately excludes ProbeType.TRACEROUTE -- traceroute is route
# analysis applied to a target generally (core.routing), not one of the
# per-service health checks these two documents describe.
SERVICE_CHECK_TYPES = frozenset({ProbeType.ICMP, ProbeType.DNS, ProbeType.TCP, ProbeType.TLS, ProbeType.HTTP})


@dataclass
class Service:
    """A generically monitored target (TASK-032), per
    architecture-overview.md SS5's Service paragraph and
    module-boundaries.md's Services section.

    NO HARDCODED PROVIDERS
    ---------------------------
    Both documents are explicit: "no hardcoded 'GitHub'/'Cloudflare'/
    'Telegram' in the domain" / "a Service is always user- or
    config-defined data, never a name baked into logic." This class
    itself enforces that structurally rather than by convention alone --
    there is no default `name`, no preset/well-known Service instances,
    and no registry of recognized providers anywhere in this module.
    Every Service that ever exists is data someone supplied (config or,
    eventually, persistence), never something `core` invented.

    FIELDS
    ----------
    `name`: a user- or config-supplied label (e.g. "My VPN provider") --
    purely a display/identification string with no semantic meaning to
    `core`.

    `host`: the address/hostname this Service's checks target.

    `enabled_checks`: which of the five checks
    (`SERVICE_CHECK_TYPES`: icmp/dns/tcp/tls/http) are enabled for this
    Service -- reuses the existing `ProbeType` enum rather than
    introducing five new boolean fields or a second, parallel
    vocabulary. Empty by default (no checks enabled yet), since "each
    optional" means a Service isn't required to enable all five.

    WHAT THIS CLASS DOES NOT DO
    --------------------------------
    Per this task's own roadmap scope (`core/models.py` only) and
    module-boundaries.md's Services section (`app`'s aggregation use
    case is a separate, later responsibility -- TASK-033), this class
    holds data only. It does not run checks, does not aggregate
    Measurements into a per-service Diagnosis, and does not call any
    `adapters/probes/*` -- module-boundaries.md is explicit that a
    Service's checks call "the exact same adapters/probes/* any other
    measurement round uses," so this class has no reason to duplicate
    or reference probe execution logic at all.
    """

    name: str
    host: str
    enabled_checks: set[ProbeType] = field(default_factory=set)

    def __post_init__(self) -> None:
        invalid = set(self.enabled_checks) - SERVICE_CHECK_TYPES
        if invalid:
            raise ValueError(
                f"Service.enabled_checks may only contain {sorted(c.value for c in SERVICE_CHECK_TYPES)}, "
                f"got invalid: {sorted(c.value for c in invalid)}"
            )
