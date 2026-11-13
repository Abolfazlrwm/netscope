# Geo location, distance and ownership (optional, offline, v0.12 + v0.14)

NetScope can tell you *where* a target lives — country, city — and, once
it knows where **you** are, **how far away** it is. It can also tell you
*whose road it sits on*: the autonomous system number and the operating
organization's name. The features are powered by MaxMind's free
**GeoLite2** databases (City for geography, ASN for ownership), read
from the files you download yourself: NetScope ships no database,
fetches none, and works fully offline once the files exist. Without
them, every command behaves exactly as before — no geography, no
ownership, no warnings, no errors.

## Why the reader is built in

NetScope reads the `.mmdb` file format with its own dependency-free
reader (see [`adr-011`](../architecture/adr-011-geoip-strategy.md)):
no `pip install` step beyond NetScope itself, no native compilation, and
the same reader works identically on Windows, Linux and macOS. You bring
the data; NetScope brings the format.

## Setup, end to end

### 1. Get a (free) MaxMind account

1. Sign up at <https://www.maxmind.com/en/geolite2/signup> — GeoLite2 is
   free; you only need an account and agreement to the
   [GeoLite2 EULA](https://www.maxmind.com/en/geolite2/eula).
2. In the account dashboard, open **Manage License Keys** and generate a
   new license key. You will also see your numeric **Account ID**.
3. The EULA's short version, and how NetScope honors it: the database
   stays your download (never redistributed by NetScope), attribution is
   required (it lives in the project's `NOTICE` file), and the database
   should be refreshed regularly. NetScope surfaces that last point
   itself: a lookup from a database older than **30 days** carries a
   visible "refresh recommended" notice in the check detail and the
   evidence reports.

### 2. Download GeoLite2-City

GeoLite2-**City** is required (Country lacks the latitude/longitude the
distance needs). From a terminal, with your own Account ID and license
key in place of the placeholders:

```bash
curl -o geolite2-city.tar.gz \
  -u "ACCOUNT_ID:LICENSE_KEY" \
  "https://download.maxmind.com/geoip/databases/GeoLite2-City/download?suffix=tar.gz"

tar -xzf geolite2-city.tar.gz
```

The archive contains `GeoLite2-City.mmdb` (plus a README and a
copyright file). Move the `.mmdb` to NetScope's data directory:

```bash
mkdir -p ~/.netscope
cp GeoLite2-City*/GeoLite2-City.mmdb ~/.netscope/
```

That is the default location; the other two files are MaxMind's own
documents (keep them if you like, NetScope does not need them).

### 3. Tell NetScope where *you* are (optional — this is what enables distance)

The target's place works with no configuration. Distance needs your
side of the question, which NetScope **never guesses**. Two ways, both
set in `~/.netscope/config.toml`:

```toml
# Option A — your public IP (looked up in the same database, once):
home_ip = "5.x.x.x"

# Option B — your coordinates, no IP at all:
home_latitude = 35.6892
home_longitude = 51.3890
```

- Coordinates win when both are set.
- `home_ip` is your *public* IP (what the internet sees — any "what is
  my IP" page shows it once; it is looked up in the same GeoLite2 file,
  offline).
- With neither, lookups still answer with the target's place and the
  distance is simply absent — never fabricated.

### 4. Use it

```console
$ netscope check example.com --checks dns
Experience score: 98/100 (excellent)

 Check  Target        Status  Latency   Loss   Jitter  Detail
 dns    example.com   ✓ ok    12.3 ms   0.0%   —       answers: 93.184.216.34 ·
                London, United Kingdom · 4,397 km from home · db 2d old
```

Geography appears wherever the IP is known:

- **`check` / `service check` / `diagnose`** — the measurement table's
  detail column, and every `--json` payload's per-measurement
  `extra.geo` block.
- **`netscope route`** — each hop's **Country** column fills from the
  same database (the field the route model reserved for it).
- **`netscope report`** (markdown and `--html`) — a **Geography**
  section listing each target's place, distance and database age as the
  *newest geo-aware measurement recorded it*; a report re-looks-up
  nothing.

## The `extra.geo` block (for scripts)

Every geo-aware measurement carries this flat, JSON-safe dict — stable
keys from v0.12:

| Key | Meaning |
|---|---|
| `ip` | the address that was looked up |
| `country` / `country_code` / `city` | the place as GeoLite2 names it (any may be `null`) |
| `latitude` / `longitude` | the record's coordinates, when present |
| `distance_km` | great-circle distance from your configured home; `null` when your home is unknown or either side lacks coordinates |
| `db` | the database's `database_type` (e.g. `GeoLite2-City`) |
| `db_age_days` | whole days since the database's build epoch |
| `stale_db` | `true` once `db_age_days` passes 30 (the refresh-rhythm notice) |

## Custom database location

Any path works — useful for a shared download or a non-default home
directory:

```toml
geo_db_path = "D:/geo/GeoLite2-City.mmdb"
```

A missing, corrupt, or unreadable database is a degraded install, not an
error: NetScope degrades to no-geo and everything else keeps working
(the same posture as the optional `ipasn.dat` ASN database).

## Network ownership: GeoLite2-ASN (v0.14)

Geography answers *where*; the ASN edition answers **whose**: which
autonomous system an address belongs to, and under whose name it is
operated. The same hand-written reader reads this edition too — one
format, two databases (`adr-013`).

Download GeoLite2-**ASN** from the same account (step 1 above), with
your own credentials:

```bash
curl -o geolite2-asn.tar.gz \
  -u "ACCOUNT_ID:LICENSE_KEY" \
  "https://download.maxmind.com/geoip/databases/GeoLite2-ASN/download?suffix=tar.gz"

tar -xzf geolite2-asn.tar.gz
mkdir -p ~/.netscope
cp GeoLite2-ASN*/GeoLite2-ASN.mmdb ~/.netscope/
```

That is the default location; override it in `config.toml` with
`asn_db_path`.

What you get once the file is in place:

- **`netscope route`** — the Network column shows the operator:
  `AS13335 Cloudflare, Inc.` The legacy `~/.netscope/ipasn.dat` text
  table (v0.2's prefix/ASN file) still works as the fallback — it
  answers with the number only, honestly without a name, and the
  database's own `autonomous_system_organization` is preferred when
  available.
- **`check` / `service check` / `diagnose`** — the detail column gains
  the same ownership sentence, and every `--json` payload's
  per-measurement `extra.asn` block carries it for scripts.
- **`netscope report`** (markdown and `--html`) — the Geography section
  becomes **Geography and ownership**, listing each target's place,
  distance, owner and database ages exactly as the newest enriched
  measurement recorded them.

### The `extra.asn` block (for scripts)

| Key | Meaning |
|---|---|
| `ip` | the address that was looked up |
| `asn` | the autonomous system number (`null` when the source knows nothing for the address) |
| `organization` | the operating organization as the database spells it; `null` when the source cannot name one (the `ipasn.dat` fallback never can) |
| `db` | the source's `database_type` (e.g. `GeoLite2-ASN`); `null` for the text table |
| `db_age_days` | whole days since the database's build epoch |
| `stale_db` | `true` once `db_age_days` passes 30 (the refresh-rhythm notice) |

## Privacy

The whole feature is local: the database file, the lookups, the distance
math. Nothing is transmitted, and the only address ever resolved against
the database is one your probes already talk to — plus your own
`home_ip`, which never leaves your machine (it is only a key into the
local file).
