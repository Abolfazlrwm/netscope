# ADR-013 — ASN Ownership: GeoLite2-ASN Through the Same Reader

**Status:** Accepted (v0.14)

## Context

Route ownership has always been a two-part answer. TASK-022 ("ASN/ISP
intelligence") shipped a longest-prefix-match lookup that can say the
NUMBER — `RouteHop.asn`, "AS13335" — but explicitly declined the NAME:
its scope note documented that `RouteHop.organization` stays unpopulated
because no reliable, license-clean, dependency-free data source for
organization names existed in that task's scope. The field has
documented itself as lookup-ready and empty ever since TASK-020.

v0.12 changed the calculus without changing a single line of the reader:
`adr-011`'s hand-written mmdb implementation (`adapters/geoip.py`) was
built adapter-agnostic on purpose — its consequences even named the
follow-up ("Future format work (e.g. GeoLite2-ASN files) can reuse the
same reader"). GeoLite2-ASN records are flat — `autonomous_system_number`
and `autonomous_system_organization` — so the number and the NAME of
every network on the path are reachable offline, from the user's own
download, under the same GeoLite2 EULA the distance feature already
satisfies.

The open question this ADR decides: **how both sources serve one
enrichment** without the app layer (or the UI) ever caring which one
answered.

## Decision

1. **One port, two implementations.** `core.ports.AsnLookup` formalizes
   the contract TASK-022's adapter always answered informally:
   `lookup(ip) -> AsnInfo | None`, with `AsnInfo` (`core/asn.py`)
   carrying the number, the organization name when the source can name
   one, the matched prefix, and the same disclosure fields `GeoInfo`
   carries (`db_type`, `db_age_days`, `stale_db`). The TASK-022 text
   table migrates onto the shared record — it answers
   `organization=None`, the honest record a "prefix, whitespace, ASN"
   file can produce — and the new `MaxMindAsnLookup` (`adapters/geoip.py`)
   reads GeoLite2-ASN through the v0.12 reader unchanged.
2. **The composition root prefers the richer source.** `_open_asn_lookup`
   wires GeoLite2-ASN (`~/.netscope/GeoLite2-ASN.mmdb`, or `asn_db_path`
   in the config) when the file exists, and falls back to the legacy
   `~/.netscope/ipasn.dat` text table when it does not. A corrupt mmdb
   falls THROUGH to the text table — a broken name database must not
   cost the numbers — and a corrupt text table wires None. Every failure
   is a degraded install, never an error a route command dies on.
3. **Enrichment fills the reserved field.** `_enrich_snapshot` now
   populates `RouteHop.organization` — its first real population since
   TASK-020 declared it lookup-ready — and target-level checks carry the
   canonical `extra["asn"]` block (`core.asn.asn_extra`), the exact
   discipline `extra["geo"]` established in v0.12.
4. **No new dependency of any kind is added** — runtime, optional extra,
   or test. The reader, the fixture writer, and the offline-test
   doctrine are all v0.12's, reused.

## EULA compliance

Identical in kind to `adr-011`, applied to the second edition: the
database is the USER's own download (NetScope never ships, downloads, or
generates one); lookups answer with display fields only (number, name);
`NOTICE`'s GeoLite2 attribution covers this edition explicitly; and the
staleness disclosure reuses `core.geo`'s `DB_STALE_AFTER_DAYS` policy —
one refresh rhythm for both GeoLite2 editions, never two numbers that
can disagree.

## Alternatives considered

- **Keep the text table only.** Rejected: it structurally cannot name
  organizations, so `RouteHop.organization` would stay a dead field and
  the roadmap's ownership promise would remain unkept while a suitable
  offline source sat one reader away.
- **`geoip2`/`maxminddb` library for the ASN file.** Rejected for the
  same reasons `adr-011` rejected them for City — native extensions, a
  public spec, and a dependency-free streak that v0.9 through v0.13 all
  kept. Introducing a C-extension package to read a format the project
  already reads would be regression dressed as convenience.
- **Team Cymru's DNS ASN service.** Rejected: it is a network round-trip
  per hop, which breaks the offline posture every enrichment feature
  shares (geo, hostnames-optional) and makes output nondeterministic in
  tests.

## Consequences

- `RouteHop.organization` is a live field: the `netscope route` table's
  Network column shows "AS13335 Cloudflare, Inc." when the mmdb source
  is wired, and the bare number when only the text table is.
- The evidence reports gain ownership facts beside the geography facts —
  one shared `asn_summary` wording across rich detail, markdown and HTML,
  the same one-shared-wording rule `geo_summary` established.
- `tests/test_lookups.py` migrates to the shared record; the legacy
  result type is gone — a pre-1.0 internal shape, corrected at the layer
  boundary where the project already once corrected pyasn's.
- The remaining roadmap item shrinks to UI polish (surfacing geo/ownership
  on the TUI's dashboard and network screens) — the data layer now feeds
  every surface the same two records.
