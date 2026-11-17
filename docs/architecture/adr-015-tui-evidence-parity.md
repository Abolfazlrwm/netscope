# ADR-015: The evidence reaches the TUI -- one copy of the fact on every screen

**Status:** Accepted (v0.16, 2026-11)
**Companion:** `adr-011-geoip-strategy.md` (disclosure discipline),
`adr-013-asn-strategy.md` (ownership sources), `adr-009`-style UI layering
(the TUI calls `app` use cases and reads domain objects, never adapters)

## Context

The enrichment pipeline established in v0.12 and v0.14 follows one
discipline: a fact is looked up exactly once, attached to the record that
carries it (`RawMeasurement.extra["geo"]` / `extra["asn"]`,
`RouteHop.asn/organization/country`), and every renderer cites the SAME
copy with the SAME shared wording (`geo_summary`, `asn_summary`,
`geo_place_label` -- "every renderer shows exactly this", `app/reports.py`).

The TUI was the exception, at four layers:

1. **Its wiring was config-blind.** The shell's lazy container build called
   `build_container()` with no config: `netscope tui` wired the geo database
   path, the user's home and the ASN source from default-constructed guesses
   while every CLI command read `config.toml`. A user with a configured
   `geo_db_path` and `home_ip` got a dashboard that silently disagreed with
   their own configuration.
2. **Its dashboard had no place for the place.** The checks table carried
   vitals (latency, loss, jitter) but nothing said where a target lives or
   whose network it sits on -- facts the round pipeline had ALREADY attached
   to every measurement before scoring and persistence.
3. **Its route table drifted.** The CLI's shared route table renders
   "AS13335 Cloudflare, Inc." in a Network column and the hop's country in
   the next one (`ui/render.py route_table`); the TUI route screen showed a
   bare AS number and no country at all. Two tables, two wordings, drifting
   apart since the day the CLI's grew (v0.14).
4. **Its network screen stopped at the interfaces.** "Where am I and whose
   road am I on" -- the question v0.12/v0.14 answer for every TARGET -- was
   never answered for the user's own line, although the same databases, the
   same config fields (`home_ip`, home coordinates) and the same summary
   wording already existed.

The post-TASK-046 roadmap reserved this item explicitly ("surfacing
geo/ownership on the TUI dashboard/network screens") and ordered it before
the export-format idea; the v0.16 cycle took it in that order.

## Decision

1. **Config reaches the shell.** `run_tui_command` loads config.toml exactly
   like every other command and hands it to `NetScopeTUI`; the shell's lazy
   `build_container(config=self.config)` passes it through. An injected
   config (the CLI's) wins; a bare shell (tests) keeps the built-in default.
   No new wiring path is invented -- the TUI simply stops being exempt.
2. **The dashboard reads the canonical `extra` block, never re-looks-up.**
   The Where column composes place (`geo_place_label`), distance and the AS
   number from `m.extra`, mirroring the detail builder's key discipline:
   a cell is a READ of an existing fact, not a second lookup. Absence renders
   an honest dash. The organization's name deliberately stays off the compact
   cell -- the reports and `--json` carry the name; the table keeps the
   number that fits a glance.
3. **Route parity with the shared wording.** The route screen's ASN column
   becomes Network ("AS" + organization, dim; bare number when the source
   cannot name one) and a Country column joins it -- cell semantics lifted
   from `route_table`, not reinvented beside it. The screen still performs
   zero lookups itself; it reads `RouteHop`'s lookup-ready fields, populated
   by the same `_enrich_snapshot` the CLI route uses.
4. **The network screen answers the local question with the same sources.**
   A "Your line" block joins the context card: config's `home_ip` looked up
   in the wired GeoLite2-City and GeoLite2-ASN databases, rendered through
   the shared `geo_summary`/`asn_summary` -- so the EULA's aging disclosure
   ("db Nd old -- refresh recommended") travels with the fact, per edition.
   Home coordinates alone produce the honest middle state ("distance-from-home
   is active; set home_ip to name your own place"). Every absence is one dim
   line: no home configured, no database wired, an unknown address, a
   database that fails to answer -- never a crash, never a fabricated place.
   The lookups run on a dedicated background worker (mmdb reads can block;
   the same reasoning as the discovery worker) and `r` re-asks them.
5. **The diagnostics screen is deliberately out of scope.** The diagnosis
   card shows PROBLEMS -- evidence lines about deviations -- not context
   metadata; geography and ownership are context, and the dashboard's Where
   column already covers the glance question. Revisit only if a diagnosis
   ever needs a place to state its case.

## Consequences

- The TUI and the CLI tell one story: a wording change to `geo_summary`/
  `asn_summary`/`geo_place_label` lands on every surface at once, and the
  route screen's docstring no longer over-promises ("ASN per hop" became
  the shared network wording).
- The config-blind shell bug class is closed at the root: any future config
  field the container wiring consumes reaches the TUI with no extra work.
- Test coverage pins BOTH sides of the discipline -- the fact (place,
  distance, AS number, organization, country) and the honest absence (the
  dash, the dim line, the name withheld from a compact cell) -- all offline
  through Textual's pilot.
- Zero new dependencies, the eighth release in a row.
