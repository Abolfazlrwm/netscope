# ADR-011 — GeoIP Strategy: Dependency-Free MaxMind DB Reader

**Status:** Accepted (v0.12)

## Context

TASK-023 ("Distance estimation") is the last route-model promise:
`RouteHop.country` has documented itself as lookup-ready since TASK-020,
and the roadmap's motivating question — *how far away is the thing I'm
talking to?* — needs offline GeoIP data. The data source was settled long
ago: MaxMind's GeoLite2, under a free-account EULA whose requirements
(attribution, no redistribution, refresh) were pre-drafted in `NOTICE` and
flagged in `adr-006` and `dependency-strategy.md` as "must be satisfied as
its own reviewable task".

The open question this ADR decides: **how NetScope reads the database**.
The roadmap row and `adr-006` anticipated `geoip2`/`maxminddb` as the
reader. Three facts changed the calculus before implementation:

1. **Precedent.** TASK-022's original `pyasn` adapter was replaced after
   it failed to install on the project's primary environment (Windows /
   Python 3.13) — "Microsoft Visual C++ 14.0 or greater is required" —
   because pyasn ships no prebuilt Windows wheel. The replacement was a
   pure-Python parser of a documented file format (`adapters/lookups.py`),
   and it has needed zero maintenance since.
2. **Policy.** `adr-006` rule 3: prefer the standard library whenever it
   is sufficient. The MaxMind DB file format is fully, publicly specified
   (maxmind.github.io/MaxMind-DB); reading it is a search-tree walk plus
   a small type decoder — ~250 lines of stdlib `struct`/`ipaddress`.
3. **Streak.** Every release since v0.9 added capability with zero new
   runtime dependencies; v0.11's own tagline was "without a single new
   dependency". `maxminddb` is a native-extension package (its fast path
   compiles C; wheels exist, but the failure mode TASK-022 documented is
   exactly the one a wheel-less platform or future Python would hit again).

## Decision

NetScope reads GeoLite2 with a **hand-written, dependency-free reader**
(`adapters/geoip.py`), not the `maxminddb`/`geoip2` libraries:

- `adapters/geoip.py` implements the public format spec: the binary
  search tree (record sizes 24/28/32), the 16-byte data-section
  separator (baked into leaf pointers), the full data-type decoder
  (including extended types and all four pointer sizes), and the
  trailing metadata map — IPv4-in-IPv6 via the reference readers'
  96-zero-bit entry walk.
- `MaxMindGeoLookup` implements the new `GeoLookup` port
  (`core/ports.py`); `core/geo.py` owns the domain types (Place,
  GeoInfo), the haversine distance, and the staleness policy
  (`DB_STALE_AFTER_DAYS` = 30, the EULA's monthly refresh rhythm).
- **No new dependency of any kind is added** — runtime, optional extra,
  or test. `netscope[geoip]` does not exist; if you have the database
  file, you have the feature.

Conformance is pinned two ways, both offline:

1. A spec-exact synthetic mmdb **writer** (`tests/mmdb_fixture.py`)
   builds test databases in the real format — GeoLite2 data itself is
   never committed, downloaded in CI, or needed at test time.
2. When the official `maxminddb` reference library happens to be
   installed locally, crosscheck tests compare both readers' answers on
   the same fixture bytes (and the implementation was validated against
   MaxMind's own published test databases during development); the
   reference library skips cleanly in CI, never a dependency.

## EULA compliance (the constraints this design was shaped by)

- NetScope **never ships, downloads, or generates** a GeoLite2 database.
  The user creates a free MaxMind account, downloads GeoLite2-City with
  their own license key, and places the file at
  `~/.netscope/GeoLite2-City.mmdb` (or points `geo_db_path` at it). No
  file, no feature — the same honest-optional posture as `ipasn.dat`.
- Lookups answer with **display fields only** (country, city,
  coordinates, distance); raw record dumps never cross the adapter
  boundary, let alone get persisted or redistributed.
- The attribution string the EULA requires now lives in `NOTICE` as an
  actual notice, not a forward reference.
- The adapter discloses the database's build age, and `core.geo`'s policy
  (> 30 days) surfaces an aging notice in the check detail and both
  evidence-report renderings — the EULA's refresh rhythm made visible.

## Consequences

- `adr-006`'s dependency table row for `pyasn`/`geoip2` is resolved as
  "superseded by ADR-011": no third-party GeoIP code is added; the
  GeoLite2 **data** EULA stands, satisfied by `NOTICE` + the runtime
  staleness disclosure.
- `dependency-strategy.md`'s Geo/IP row is updated to match.
- A fixture writer now exists in `tests/` — test tooling only, never
  shipped; packaging tests keep it out of distributions.
- Future format work (e.g. GeoLite2-ASN files) can reuse the same reader;
  the tree/decoder half of `adapters/geoip.py` is adapter-agnostic.
