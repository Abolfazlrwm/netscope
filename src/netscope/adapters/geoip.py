"""
netscope.adapters.geoip

Offline GeoIP lookup adapters (v0.12 TASK-023, extended v0.14): a
dependency-free reader for the MaxMind DB (".mmdb") file format and
two adapters that turn GeoLite2 database files into core ports --
`MaxMindGeoLookup` (GeoLite2-City -> `core.ports.GeoLookup`) and
`MaxMindAsnLookup` (GeoLite2-ASN -> `core.ports.AsnLookup`, adr-013).
One reader, two databases, one story: the same publicly specified
binary format, parsed with the same ~250 lines of stdlib
`struct`/`ipaddress`, answering "where is it" and "whose road is it"
alike.

WHY A HAND-WRITTEN READER, NOT A LIBRARY
-----------------------------------------
The project's dependency policy (adr-006, rule 3) prefers the
standard library whenever it is sufficient, and this project has
already rejected one C-extension GeoIP dependency for exactly the
reasons that apply here too: `pyasn` (TASK-022) failed to install on
the project's primary Windows/Python 3.13 environment -- "Microsoft
Visual C++ 14.0 or greater is required" -- because it ships no
prebuilt wheel. `maxminddb` and `geoip2` fare better on wheels, but
both are still native extensions (the fast path compiles C), and the
GeoLite2 file format itself is a fully, publicly specified binary
format (maxmind.github.io/MaxMind-DB). Reading it is ~250 lines of
stdlib `struct`/`ipaddress` -- the same "a handful of lines against a
well-understood format" reasoning that chose stdlib sockets over
scapy for TCP/TLS (adr-002). NetScope therefore reads GeoLite2 with
zero new dependencies, keeping `pip install netscope` compiling
nothing, on every platform, exactly as v0.9 through v0.11 did.

The format implementation below is validated two ways in tests: a
synthetic fixture writer (tests/mmdb_fixture.py) round-trips every
data type this reader decodes, and -- when the official `maxminddb`
reference library happens to be installed -- the reader's answers are
compared against that library's on the same fixture bytes. Neither
MaxMind nor any third-party reader is a runtime dependency.

EULA COMPLIANCE (the constraint this module was scoped around)
---------------------------------------------------------------
GeoLite2 data is NOT open-source; it ships under MaxMind's GeoLite2
EULA. This module honors that by construction:

- It never downloads, fetches, generates, or ships a database. The
  user creates a (free) MaxMind account, downloads GeoLite2-City
  themselves with their own license key, and points NetScope at the
  file (default `~/.netscope/GeoLite2-City.mmdb`). Without that file
  nothing here runs -- a missing database is a normal, silent
  degradation, exactly like the ASN database before it. GeoLite2-ASN
  (v0.14) keeps the identical discipline with its own file, the
  user's own download, by default `~/.netscope/GeoLite2-ASN.mmdb`.
- It never writes or redistributes database content: lookups answer
  with a handful of display fields (country, city, coordinates,
  distance), never with raw record dumps.
- It reports the database's build date so the UI can surface the
  aging notice the EULA's refresh rhythm asks for (the policy lives
  in `core.geo.DB_STALE_AFTER_DAYS`; see NOTICE for the attribution
  line the EULA requires).

WHAT THIS MODULE DOES NOT DO
-----------------------------
- No network access of any kind: the mmdb file is read from local
  disk and never written.
- No hostname resolution inside `MaxMindGeoLookup.lookup` -- it takes
  an IP, per the port. Target-string-to-IP resolution is a separate
  helper (`resolve_target_ip`) the app layer may call first; keeping
  the two apart is what makes a pure-IP GeoLookup testable without a
  resolver.
- It does not decide staleness (`core.geo` owns the policy), compute
  distance policy defaults, or choose the user's home location
  (config owns that); it only reports what the database and the
  constructor were given.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netscope.core.asn import AsnInfo
from netscope.core.geo import GeoInfo, Place, place_distance_km
from netscope.core.models import utcnow

# The metadata section marker -- the metadata map begins immediately
# after its LAST occurrence in the file (the spec requires searching
# backwards, so a record that happens to contain the marker bytes
# cannot misdirect the parser).
_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"

# Null bytes between the search tree and the data section. The spec
# fixes this at 16 for every database written in binary format
# version 2.
_DATA_SECTION_SEPARATOR = 16

# Data type codes (spec section "Data Types"). Extended types
# (control byte type field 0) encode `actual_type - 7` in a second
# byte; they are listed here under their actual numbers.
_TYPE_EXTENDED = 0
_TYPE_POINTER = 1
_TYPE_UTF8_STRING = 2
_TYPE_DOUBLE = 3
_TYPE_BYTES = 4
_TYPE_UINT16 = 5
_TYPE_UINT32 = 6
_TYPE_MAP = 7
_TYPE_INT32 = 8
_TYPE_UINT64 = 9
_TYPE_UINT128 = 10
_TYPE_ARRAY = 11
_TYPE_BOOL = 14
_TYPE_FLOAT = 15

# Search-tree record sizes this reader accepts. Real GeoLite2
# databases ship record_size 24 (Country) or 28 (City); 32 is part of
# the spec and cheap to support, and readers are required to handle
# all three.
_NODE_SIZE_BYTES = {24: 6, 28: 7, 32: 8}


class MMDBFormatError(ValueError):
    """The file is not a MaxMind DB this reader can parse -- bad
    marker, unknown record size, truncated sections. Raised by the
    reader; the adapter wraps it (and OS errors) into
    GeoLookupUnavailableError so callers meet one failure type."""


class GeoLookupUnavailableError(RuntimeError):
    """Raised when an mmdb-backed database cannot be read/parsed at
    all -- missing file, corrupt file, not an mmdb file. Raised by
    BOTH mmdb adapters (the GeoIP one since v0.12, the ASN one since
    v0.14 -- same reader, same failure type), and distinct from a
    normal "this address is not in the database" lookup result (which
    is None, not an error). Mirrors
    `adapters.lookups.AsnLookupUnavailableError`, the text-table
    source's own failure type."""


@dataclass
class MMDBMetadata:
    """The metadata-section fields this reader and the adapter need.
    Only `record_size`, `node_count` and `ip_version` are
    load-bearing; the rest ride along for the adapter's disclosure
    fields (`database_type`, `build_epoch`)."""

    node_count: int
    record_size: int
    ip_version: int
    database_type: str | None = None
    build_epoch: int | None = None


class MMDBReader:
    """A minimal, read-only MaxMind DB (mmdb) file reader.

    Parses the three file sections per the public spec: the binary
    search tree (records of 24/28/32 bits), the 16-byte separator,
    the data section (pointer/type/size encoded values), and the
    trailing metadata map. `get(ip)` walks the tree bit by bit and
    decodes the record it lands on, or returns None when the tree
    says "no data" -- the spec's own vocabulary for an unknown
    address.

    IPv4 addresses inside an ip_version=6 database are resolved the
    way the official readers do: the tree is entered at the node
    reached by walking 96 zero bits (the ::/96 prefix), then the 32
    address bits -- computed once, lazily, per file.
    """

    def __init__(self, path: str | Path) -> None:
        self._buffer = Path(path).read_bytes()
        self._metadata = self._parse_metadata()
        meta = self._metadata
        self._node_size = _NODE_SIZE_BYTES[meta.record_size]
        self._tree_size = meta.node_count * self._node_size
        self._data_section_offset = self._tree_size + _DATA_SECTION_SEPARATOR
        self._ipv4_entry: int | None = None  # None = not computed yet

    # -- metadata -------------------------------------------------

    def _parse_metadata(self) -> MMDBMetadata:
        marker_at = self._buffer.rfind(_METADATA_MARKER)
        if marker_at < 0:
            raise MMDBFormatError("no MaxMind metadata marker found -- not an mmdb file")
        metadata_start = marker_at + len(_METADATA_MARKER)
        try:
            # Pointers inside the metadata section are relative to the
            # metadata map itself (reference readers construct the
            # metadata decoder with the metadata's own start as its
            # pointer base) -- shared keys like "en" point backward
            # within metadata, never into the data section.
            raw, _ = self._decode_at(metadata_start, pointer_base=metadata_start)
        except (IndexError, struct.error) as exc:
            raise MMDBFormatError(f"metadata section is truncated or corrupt: {exc}") from exc
        if not isinstance(raw, dict):
            raise MMDBFormatError("metadata section did not decode to a map")
        try:
            meta = MMDBMetadata(
                node_count=int(raw["node_count"]),
                record_size=int(raw["record_size"]),
                ip_version=int(raw["ip_version"]),
                database_type=raw.get("database_type") if isinstance(raw.get("database_type"), str) else None,
                build_epoch=int(raw["build_epoch"]) if isinstance(raw.get("build_epoch"), int) else None,
            )
        except KeyError as exc:
            raise MMDBFormatError(f"metadata is missing required field: {exc}") from exc
        if meta.record_size not in _NODE_SIZE_BYTES:
            raise MMDBFormatError(f"unsupported record_size: {meta.record_size}")
        if meta.ip_version not in (4, 6):
            raise MMDBFormatError(f"unsupported ip_version: {meta.ip_version}")
        return meta

    @property
    def metadata(self) -> MMDBMetadata:
        return self._metadata

    # -- search tree ----------------------------------------------

    def _read_record(self, node: int, index: int) -> int:
        """One tree record: `index` 0 = left (bit 0), 1 = right (bit
        1), big-endian. For record_size 28 the two 28-bit records
        share a byte -- the reference reader's own byte math is
        mirrored here (left keeps the shared byte's high nibble, the
        right record's high 24 bits sit in the last three bytes with
        the shared byte's low nibble as its low bits)."""
        base = node * self._node_size
        b = self._buffer
        if self._metadata.record_size == 24:
            off = base + index * 3
            return int.from_bytes(b[off : off + 3], "big")
        if self._metadata.record_size == 28:
            if index == 0:
                record = int.from_bytes(b[base : base + 4], "big")
                return (record >> 8) | ((record & 0xF0) << 20)
            record = int.from_bytes(b[base + 3 : base + 7], "big")
            return record & 0x0FFFFFFF
        off = base + index * 4
        return int.from_bytes(b[off : off + 4], "big")

    def _ipv4_entry_node(self) -> int:
        """The node an IPv4 address enters at inside an IPv6 tree:
        follow 96 zero bits from the root (::/96 -- where GeoLite2
        stores IPv4-mapped data). Mirrors the reference readers
        exactly, including their edge semantics: if the walk lands on
        a leaf (not-found or a ::/96-wide data record), that value is
        returned as-is and the generic walk resolves it -- a database
        whose IPv4 section is one catch-all record still answers every
        IPv4 lookup with it."""
        if self._ipv4_entry is None:
            node = 0
            for _ in range(96):
                if node >= self._metadata.node_count:
                    break
                node = self._read_record(node, 0)
            self._ipv4_entry = node
        return self._ipv4_entry

    def get(self, ip_address: str) -> dict[str, Any] | None:
        """Look up one address; None when the database has no record
        for it. Raises ValueError on a syntactically invalid address
        (caller programming error) and MMDBFormatError when the tree
        ends mid-address -- a structurally corrupt database."""
        addr = ipaddress.ip_address(ip_address)
        meta = self._metadata
        if meta.ip_version == 4:
            if addr.version != 4:
                raise MMDBFormatError("IPv6 address against an IPv4-only database")
            node, num_bits = 0, 32
        elif addr.version == 4:
            node, num_bits = self._ipv4_entry_node(), 32
        else:
            node, num_bits = 0, 128
        value = int(addr)
        position = 0
        while position < num_bits and node < meta.node_count:
            bit = (value >> (num_bits - 1 - position)) & 1
            node = self._read_record(node, bit)
            position += 1
        if node == meta.node_count:
            return None  # the empty record: no data for this address
        if node > meta.node_count:
            # A data leaf: the separator is baked into the pointer
            # value by writers (value = node_count + 16 + offset), so
            # the file offset is value - node_count measured from the
            # tree's end -- the arithmetic the reference readers use.
            data, _ = self._decode_at(self._tree_size + node - meta.node_count, pointer_base=self._data_section_offset)
            return data if isinstance(data, dict) else None
        raise MMDBFormatError("search tree ends mid-address -- corrupt database")

    # -- data section decoder --------------------------------------
    #
    # Every decode returns `(value, next_offset)`. The offset tuple is
    # what makes nested structures (maps of maps, arrays of pointers)
    # work without hidden state: each level consumes exactly what it
    # declares and hands the next offset back up.

    def _decode_at(self, offset: int, pointer_base: int) -> tuple[Any, int]:
        """Decode one value at an absolute buffer offset. `pointer_base`
        is where type-1 pointers resolve from: the data section start
        for tree-leaf payloads, the metadata map's own start inside the
        metadata section (reference-reader semantics). Every decode
        returns `(value, next_offset)` -- the offset tuple is what
        makes nested structures work without hidden state."""
        ctrl = self._buffer[offset]
        offset += 1
        type_code = ctrl >> 5
        size = ctrl & 0x1F

        if type_code == _TYPE_EXTENDED:
            type_code = self._buffer[offset] + 7
            offset += 1

        if type_code == _TYPE_POINTER:
            return self._decode_pointer(size, offset, pointer_base)

        if size == 29:
            size = 29 + self._buffer[offset]
            offset += 1
        elif size == 30:
            size = 285 + int.from_bytes(self._buffer[offset : offset + 2], "big")
            offset += 2
        elif size == 31:
            size = 65821 + int.from_bytes(self._buffer[offset : offset + 3], "big")
            offset += 3

        b = self._buffer
        if type_code == _TYPE_UTF8_STRING:
            return b[offset : offset + size].decode("utf-8"), offset + size
        if type_code == _TYPE_BYTES:
            return b[offset : offset + size], offset + size
        if type_code in (_TYPE_UINT16, _TYPE_UINT32, _TYPE_UINT64, _TYPE_UINT128):
            return int.from_bytes(b[offset : offset + size], "big"), offset + size
        if type_code == _TYPE_INT32:
            return int.from_bytes(b[offset : offset + size], "big", signed=True), offset + size
        if type_code == _TYPE_DOUBLE:
            return struct.unpack("!d", b[offset : offset + 8])[0], offset + 8
        if type_code == _TYPE_FLOAT:
            return struct.unpack("!f", b[offset : offset + 4])[0], offset + 4
        if type_code == _TYPE_BOOL:
            if size == 2:
                raise MMDBFormatError("boolean reserved encoding (size 2)")
            return size == 1, offset
        if type_code == _TYPE_MAP:
            result: dict[str, Any] = {}
            for _ in range(size):
                key, offset = self._decode_at(offset, pointer_base)
                if not isinstance(key, str):
                    raise MMDBFormatError("map key is not a string")
                value, offset = self._decode_at(offset, pointer_base)
                result[key] = value
            return result, offset
        if type_code == _TYPE_ARRAY:
            items: list[Any] = []
            for _ in range(size):
                item, offset = self._decode_at(offset, pointer_base)
                items.append(item)
            return items, offset
        if type_code == 13:  # end marker (deprecated) -- decodes to None
            return None, offset
        if type_code == 12:  # data cache container (deprecated) -- opaque
            return b[offset : offset + size], offset + size
        raise MMDBFormatError(f"unsupported data type code: {type_code}")

    def _decode_pointer(self, size_field: int, offset: int, pointer_base: int) -> tuple[Any, int]:
        """Pointers reuse the 5-bit size field: its top two bits
        select how many bytes follow (1-4) and its low three bits are
        the pointer's HIGH bits. Sizes 1 and 2 carry a fixed bias
        (2048 / 526336) per the spec's pointer table; the resolved
        value is an offset relative to `pointer_base` -- the data
        section start for tree payloads, the metadata start for
        metadata-internal pointers."""
        selector = (size_field >> 3) & 0x3
        b = self._buffer
        if selector == 0:
            pointer = ((size_field & 0x7) << 8) | b[offset]
            end = offset + 1
            bias = 0
        elif selector == 1:
            pointer = ((size_field & 0x7) << 16) | (b[offset] << 8) | b[offset + 1]
            end = offset + 2
            bias = 2048
        elif selector == 2:
            pointer = ((size_field & 0x7) << 24) | (b[offset] << 16) | (b[offset + 1] << 8) | b[offset + 2]
            end = offset + 3
            bias = 526336
        else:
            pointer = int.from_bytes(b[offset : offset + 4], "big")
            end = offset + 4
            bias = 0
        # The enclosing map/array continues AFTER THE POINTER BYTES,
        # never after the pointed-to value -- the pointer is a
        # reference, not an inline payload (getting this wrong
        # resynchronizes the stream to the target's end, which only
        # files that actually use pointers expose).
        value, _ = self._decode_at(pointer_base + pointer + bias, pointer_base)
        return value, end


class MaxMindGeoLookup:
    """`GeoLookup` port adapter over a local GeoLite2 database file
    (see this module's docstring for why the file is the user's, and
    the EULA notes that scope this adapter to display fields only).

    `home` is the user's own `Place` -- config-supplied coordinates
    or a home IP looked up in this same database (resolved by the
    composition root, never guessed here). When both sides carry
    coordinates, `lookup()` adds the great-circle distance via
    `core.geo.place_distance_km`; otherwise `distance_km` stays None
    -- an uncomputable distance is disclosed, never approximated.

    Construction reads and parses the whole file eagerly, so an
    unusable database fails at wiring time (the composition root
    degrades to no-geo, like a missing ASN database) rather than on
    the first check command. A database that passes construction but
    corrupts mid-walk degrades per-lookup to None -- the enrichment
    layer treats every lookup failure as "no geography for this
    address", never as an error a check command should die on.
    """

    def __init__(self, db_path: str | Path, home: Place | None = None) -> None:
        try:
            self._reader = MMDBReader(db_path)
        except (OSError, MMDBFormatError) as exc:
            raise GeoLookupUnavailableError(f"GeoIP database unusable: {exc}") from exc
        self.home = home
        meta = self._reader.metadata
        self.db_type: str | None = meta.database_type
        self.db_age_days: int | None = None
        if meta.build_epoch is not None:
            age_seconds = utcnow().timestamp() - meta.build_epoch
            self.db_age_days = max(0, int(age_seconds // 86400))

    def lookup(self, ip_address: str) -> GeoInfo | None:
        """One address -> its geography (with distance from home when
        computable), or None when the database does not know the
        address. ValueError on an invalid address -- the port's
        documented split between caller error and normal absence."""
        ipaddress.ip_address(ip_address)  # validate; ValueError propagates per the port contract
        try:
            record = self._reader.get(ip_address)
        except MMDBFormatError:
            # A tree that passed metadata but corrupts mid-walk: this
            # ADDRESS has no geography we can honestly state. Degrade
            # to None -- enrichment is best-effort display metadata.
            return None
        if record is None:
            return None
        place = _extract_place(record)
        if place is None:
            return None
        return GeoInfo(
            ip_address=ip_address,
            place=place,
            distance_km=place_distance_km(self.home, place),
            db_type=self.db_type,
            db_age_days=self.db_age_days,
        )


class MaxMindAsnLookup:
    """`AsnLookup` port adapter over a local GeoLite2-ASN database
    file (v0.14, adr-013) -- the ownership answer the TASK-022 text
    table could never give: the AS number AND the operating
    organization's name, offline, through the same reader the City
    database uses.

    The two GeoLite2 editions share one contract shape and differ
    only in their records: an ASN record is flat --
    `autonomous_system_number` and `autonomous_system_organization`
    -- with no names map and no coordinates, so extraction is a
    couple of type-checked reads rather than a place walk. A record
    without a number answers None (an ownership claim with no
    autonomous system behind it is not a claim this adapter will
    carry); a number without a name is carried, honestly bare.

    Failure semantics are `MaxMindGeoLookup`'s, verbatim: eager
    construction so an unusable file fails at wiring time, per-lookup
    degradation to None for a file that corrupts mid-walk, ValueError
    for a syntactically invalid address.
    """

    def __init__(self, db_path: str | Path) -> None:
        try:
            self._reader = MMDBReader(db_path)
        except (OSError, MMDBFormatError) as exc:
            raise GeoLookupUnavailableError(f"ASN database unusable: {exc}") from exc
        meta = self._reader.metadata
        self.db_type: str | None = meta.database_type
        self.db_age_days: int | None = None
        if meta.build_epoch is not None:
            age_seconds = utcnow().timestamp() - meta.build_epoch
            self.db_age_days = max(0, int(age_seconds // 86400))

    def lookup(self, ip_address: str) -> AsnInfo | None:
        """One address -> its autonomous system and operator, or None
        when the database does not know the address. ValueError on an
        invalid address -- the port's documented split between caller
        error and normal absence."""
        ipaddress.ip_address(ip_address)  # validate; ValueError propagates per the port contract
        try:
            record = self._reader.get(ip_address)
        except MMDBFormatError:
            # Corrupt mid-walk: this ADDRESS gets no ownership claim.
            return None
        if record is None:
            return None
        asn = record.get("autonomous_system_number")
        organization = record.get("autonomous_system_organization")
        if not isinstance(asn, int) or isinstance(asn, bool):
            return None
        return AsnInfo(
            ip_address=ip_address,
            asn=asn,
            organization=organization if isinstance(organization, str) else None,
            db_type=self.db_type,
            db_age_days=self.db_age_days,
        )


def _extract_place(record: dict[str, Any]) -> Place | None:
    """GeoLite2-City record -> `Place`, the handful of display fields
    the feature discloses. Country prefers the address's registration
    country (`country`), falling back to `registered_country` -- the
    field MaxMind itself designates for "where this address is
    registered, when the country block is unknown". Names are read in
    English when present, else the first the record carries. A record
    with no geography at all yields None (nothing to disclose)."""
    country_block = record.get("country")
    if not isinstance(country_block, dict) or not country_block:
        country_block = record.get("registered_country")
    country = _name_of(country_block)
    country_code = country_block.get("iso_code") if isinstance(country_block, dict) else None
    city = _name_of(record.get("city"))
    location = record.get("location")
    latitude = longitude = None
    if isinstance(location, dict):
        lat_raw, lon_raw = location.get("latitude"), location.get("longitude")
        if isinstance(lat_raw, (int, float)) and isinstance(lon_raw, (int, float)):
            latitude, longitude = float(lat_raw), float(lon_raw)
    place = Place(
        country=country,
        country_code=country_code if isinstance(country_code, str) else None,
        city=city,
        latitude=latitude,
        longitude=longitude,
    )
    if place.country is None and place.city is None and place.latitude is None:
        return None
    return place


def _name_of(block: Any) -> str | None:
    """`names` map -> one display name: English first, else the first
    string value the record carries."""
    if not isinstance(block, dict):
        return None
    names = block.get("names")
    if not isinstance(names, dict):
        return None
    english = names.get("en")
    if isinstance(english, str):
        return english
    for value in names.values():
        if isinstance(value, str):
            return value
    return None


class TargetIpResolver:
    """The target-string-to-IP step of geo enrichment, as a wired
    adapter (the same injection pattern as HostnameResolver): the app
    layer asks the container for an IP and never imports this module
    itself -- use_cases is architecture-test-locked to app/core
    imports, so I/O helpers reach it only through injected
    dependencies.

    Resolution order: an IP literal wins; else a DNS probe's
    already-recorded answers are scanned (the check pipeline resolved
    that host seconds ago -- re-asking the resolver for a fact already
    on screen would be waste); else one stdlib `getaddrinfo` call.
    None when nothing resolves -- no IP, no geography, no warning:
    enrichment is best-effort by design.
    """

    def resolve(self, target: str, extra: dict | None = None) -> str | None:
        return resolve_target_ip(target, extra)


def resolve_target_ip(target: str, extra: dict | None = None) -> str | None:
    """The target-string-to-IP step of geo enrichment: an IP literal
    wins, else a DNS probe's already-recorded answers are scanned
    (the check pipeline usually resolved the host seconds ago --
    re-asking the resolver for a fact already on screen would be
    waste), else one stdlib `getaddrinfo` call. None when nothing
    resolves -- no IP, no geography, no warning: enrichment is
    best-effort by design.

    `TargetIpResolver.resolve` is the container-injected entry point;
    this function is its implementation and the unit-test surface.
    """
    candidate = target.strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    if extra:
        answers = extra.get("answers")
        if isinstance(answers, list):
            for answer in answers:
                if not isinstance(answer, str):
                    continue
                for token in answer.split():
                    try:
                        return str(ipaddress.ip_address(token))
                    except ValueError:
                        continue
    try:
        infos = socket.getaddrinfo(candidate, None)
    except OSError:
        return None
    for info in infos:
        addr = info[4][0]
        try:
            return str(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return None
