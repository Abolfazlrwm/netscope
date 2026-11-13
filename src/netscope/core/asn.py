"""
netscope.core.asn

Network-ownership enrichment policy (v0.14): which autonomous system a
path's addresses belong to, and under whose name it is operated -- the
sibling of `core.geo`'s "where is it" with the question "whose road is
it". The route model has carried `RouteHop.organization` as a
lookup-ready placeholder since TASK-020, next to the ASN number
TASK-022 already answered; v0.12's mmdb reader made organization names
reachable offline (GeoLite2-ASN), and this module gives that answer its
shared, dependency-free record.

WHAT THIS MODULE OWNS
---------------------
- `AsnInfo`: one IP's ownership as a lookup produced it -- the AS
  number, the operating organization when the database names one, the
  matched prefix, plus the bookkeeping a UI needs to disclose the
  lookup honestly (which database, how old). Plain data, no behavior
  beyond a `matched` convenience.
- `DB_STALE_AFTER_DAYS` reuse: `AsnInfo.stale_db` borrows
  `core.geo`'s staleness policy unchanged -- GeoLite2 editions refresh
  on the same EULA rhythm, and two "how old is too old" numbers would
  be two chances to disagree.
- `asn_extra`: the canonical `RawMeasurement.extra["asn"]` shape. One
  builder, so every surface that WRITES ownership (the app-layer
  enrichment) and every surface that READS it (rich render, the
  evidence reports, the --json payload's extra block) shares one key
  set -- the exact discipline `core.geo.geo_extra` established for
  geography in v0.12.

WHY THIS LIVES IN CORE
----------------------
Staleness and record shape are pure domain policy over domain types --
no sockets, no file I/O, no mmdb byte parsing (the same split
core/geo.py draws and core/loss.py before it). Reading an actual
GeoLite2-ASN file is infrastructure and lives in
`adapters/geoip.py` (`MaxMindAsnLookup`), behind the `AsnLookup` port
in `core/ports.py`; the legacy text-table adapter
(`adapters/lookups.py`, TASK-022) satisfies the same port and answers
with `organization=None` -- a number without a name is the honest
record that source can produce.

WHAT THIS MODULE DOES NOT DO
-----------------------------
- It does not say which database is "better": source preference (the
  mmdb file when present, the text table as fallback) is the
  composition root's wiring decision, not policy in core.
- It does not resolve hostnames to IPs -- enrichment asks for an IP
  and gets one or nothing, per the same contract `GeoLookup` keeps.
- It does not judge organizations: a name is reported as the database
  spells it, never normalized, abbreviated, or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

# The staleness policy is geography's, on purpose: both GeoLite2
# editions ship under the same EULA with the same refresh rhythm, so
# one number keeps the two aging notices honest and in step.
from netscope.core.geo import DB_STALE_AFTER_DAYS  # noqa: F401 -- re-exported for adapters' disclosure docs


@dataclass
class AsnInfo:
    """One IP's network ownership, as one lookup produced it.

    `organization` is None whenever the source cannot name one -- the
    legacy text table (TASK-022) never could, and a GeoLite2-ASN
    record may carry a number without a name. An organization field
    that renders "unknown" would be a fabricated answer; None is the
    honest one, and the UI renders the bare AS number when only the
    number exists.
    """

    ip_address: str
    asn: int | None = None
    organization: str | None = None
    prefix: str | None = None
    db_type: str | None = None
    db_age_days: int | None = None

    @property
    def matched(self) -> bool:
        """Whether the lookup actually identified a network. An AsnInfo
        without an AS number is not a match -- it exists only because
        a caller received the record from a source that answers with
        objects; absence of a number means absence of ownership
        evidence."""
        return self.asn is not None

    @property
    def stale_db(self) -> bool:
        """Whether the database this answer came from has aged past the
        refresh rhythm the EULA asks for. None age (unknown metadata --
        the text table carries no build date) is not stale: absence of
        evidence is not evidence of age, the same rule `GeoInfo.stale_db`
        keeps."""
        return self.db_age_days is not None and self.db_age_days > DB_STALE_AFTER_DAYS


def asn_extra(asn: AsnInfo) -> dict:
    """The canonical `RawMeasurement.extra["asn"]` dict -- flat JSON
    scalars only, so it survives persistence (extra_json), the --json
    payload's `_json_safe` pass, and the evidence reports without any
    serializer special-casing it, exactly like `geo_extra`.

    Key set is stable API from v0.14: scripts that read
    `extra["asn"]["organization"]` read the documented shape, not a
    happy accident of one release's dict literal. The matched prefix
    deliberately stays off this payload -- it is lookup plumbing, not
    an ownership fact a report needs; the route's `--json` hops carry
    the number and name where the question was per-hop anyway.
    """
    return {
        "ip": asn.ip_address,
        "asn": asn.asn,
        "organization": asn.organization,
        "db": asn.db_type,
        "db_age_days": asn.db_age_days,
        "stale_db": asn.stale_db,
    }
