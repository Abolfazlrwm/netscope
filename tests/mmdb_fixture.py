"""
tests.mmdb_fixture

A minimal MaxMind DB (mmdb) WRITER used only to build synthetic test
databases for `netscope.adapters.geoip` -- the offline-test answer to
"a GeoLite2 fixture without redistributing GeoLite2": the real
database is never committed, downloaded in CI, or used at test time.
Instead, tests encode a handful of synthetic networks in the real,
publicly specified file format and point the reader at those bytes.

The writer implements the same spec the production reader parses
(tree + 16-byte separator + data section + metadata), covering every
data type NetScope's records and metadata actually use (maps, arrays,
utf8 strings, unsigned ints, doubles, booleans) at record sizes 24,
28 and 32. It is test tooling, not a general-purpose mmdb writer: no
pointers are emitted (pointers are an optimization, not a structural
requirement -- the production reader's pointer path is exercised
against MaxMind's own published test databases whenever the official
`maxminddb` reference library is installed locally, see
test_geoip_adapter.py's crosscheck).

Kept in `tests/` deliberately: it exists to make tests offline, and
shipping a fixture writer inside the package would suggest a
capability (writing GeoLite2-shaped files) the project deliberately
does not have.
"""

from __future__ import annotations

import ipaddress
import struct

_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
_DATA_SECTION_SEPARATOR = 16

_TYPE_UTF8_STRING = 2
_TYPE_DOUBLE = 3
_TYPE_UINT16 = 5
_TYPE_UINT32 = 6
_TYPE_MAP = 7
_TYPE_UINT64 = 9
_TYPE_ARRAY = 11
_TYPE_BOOL = 14


def _encode_size_payload(type_code: int, size: int, payload: bytes) -> bytes:
    """Control byte + size encoding per the spec's size table. The
    3-bit type field only holds 0-7: types 1-7 encode directly;
    types >= 8 (uint64, array, boolean, ...) are "extended" -- the
    control byte's type field is 0 and one extra byte carries
    `type - 7`, BEFORE any size-extension bytes."""
    prefix = b""
    field_type = type_code
    if type_code > 7:
        prefix = bytes([type_code - 7])
        field_type = 0
    if size < 29:
        return bytes([(field_type << 5) | size]) + prefix + payload
    if size < 29 + 256:
        return bytes([(field_type << 5) | 29]) + prefix + bytes([size - 29]) + payload
    if size < 285 + 65536:
        return bytes([(field_type << 5) | 30]) + prefix + (size - 285).to_bytes(2, "big") + payload
    return bytes([(field_type << 5) | 31]) + prefix + (size - 65821).to_bytes(3, "big") + payload


def encode_utf8_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return _encode_size_payload(_TYPE_UTF8_STRING, len(raw), raw)


def encode_uint(value: int) -> bytes:
    """Smallest unsigned int type that fits, per the spec's unsigned
    encodings (uint16/uint32/uint64). Zero encodes as zero bytes --
    the canonical form, which the reader must accept."""
    if value < (1 << 16):
        size = max(0, (value.bit_length() + 7) // 8)
        return _encode_size_payload(_TYPE_UINT16, size, value.to_bytes(size, "big") if size else b"")
    if value < (1 << 32):
        size = (value.bit_length() + 7) // 8
        return _encode_size_payload(_TYPE_UINT32, size, value.to_bytes(size, "big"))
    size = (value.bit_length() + 7) // 8
    return _encode_size_payload(_TYPE_UINT64, size, value.to_bytes(size, "big"))


def encode_uint_of_type(type_code: int, value: int) -> bytes:
    """One specific unsigned type with a minimal-width payload -- the
    metadata section's convention: reference writers fix each
    metadata field's declared type (build_epoch is a uint64 even when
    its value would fit a uint32) and readers' strict validators
    check the declared type, not just the value."""
    size = max(0, (value.bit_length() + 7) // 8)
    payload = value.to_bytes(size, "big") if size else b""
    return _encode_size_payload(type_code, size, payload)


def encode_map_raw(pairs: list[tuple[str, bytes]]) -> bytes:
    """A map whose values are ALREADY encoded -- the escape hatch the
    metadata section uses to control each field's declared type."""
    body = b"".join(encode_utf8_string(key) + value for key, value in pairs)
    return _encode_size_payload(_TYPE_MAP, len(pairs), body)


def encode_double(value: float) -> bytes:
    return _encode_size_payload(_TYPE_DOUBLE, 8, struct.pack("!d", value))


def encode_boolean(value: bool) -> bytes:
    return bytes([(_TYPE_BOOL << 5) | (1 if value else 0)])


def encode_map(value: dict[str, object]) -> bytes:
    body = b"".join(encode_utf8_string(k) + encode_value(v) for k, v in value.items())
    return _encode_size_payload(_TYPE_MAP, len(value), body)


def encode_array(value: list[object]) -> bytes:
    body = b"".join(encode_value(item) for item in value)
    return _encode_size_payload(_TYPE_ARRAY, len(value), body)


def encode_value(value: object) -> bytes:
    if isinstance(value, str):
        return encode_utf8_string(value)
    if isinstance(value, bool):
        return encode_boolean(value)
    if isinstance(value, int):
        return encode_uint(value)
    if isinstance(value, float):
        return encode_double(value)
    if isinstance(value, dict):
        return encode_map(value)
    if isinstance(value, list):
        return encode_array(value)
    raise TypeError(f"fixture writer cannot encode {type(value).__name__}")


class _Node:
    """One search-tree node: two records, each either a child node,
    a leaf reference ("data", data offset), or unfilled (None -- the
    spec's 0, not-found)."""

    __slots__ = ("left", "right")

    def __init__(self) -> None:
        self.left: _Node | tuple[str, int] | None = None
        self.right: _Node | tuple[str, int] | None = None


def build_mmdb(
    networks: dict[str, dict[str, object]],
    *,
    build_epoch: int,
    database_type: str = "GeoLite2-City",
    ip_version: int = 4,
    record_size: int = 24,
    description: str = "NetScope synthetic offline fixture -- not a MaxMind product",
) -> bytes:
    """Build a spec-conformant .mmdb file from `{network: record}`.

    Layout per the spec: [search tree][16 null bytes][data section]
    [metadata marker][metadata map]. Leaf records point at
    `node_count + data_offset`; the data section is written once per
    network in sorted-network order (deterministic bytes for a
    deterministic fixture).
    """
    if ip_version == 4:
        for network in networks:
            if ipaddress.ip_network(network).version != 4:
                raise ValueError(f"IPv6 network {network} against ip_version=4 fixture")

    # -- data section first: leaf offsets depend on nothing, while
    # tree leaf values depend on node_count, which depends on the
    # whole tree.
    encoded: dict[str, int] = {}
    chunks: list[bytes] = []
    offset = 0
    for network in sorted(networks):
        payload = encode_value(networks[network])
        encoded[network] = offset
        chunks.append(payload)
        offset += len(payload)
    data_blob = b"".join(chunks)

    # -- search tree: insert each network, bit by bit, MSB first.
    # `nodes` IS the serialization order: a node's index is its
    # position in this list, exactly what in-file record values
    # must reference. IPv4 networks in an ip_version=6 fixture are
    # stored under the ::/96 prefix (96 zero bits, then the address),
    # where GeoLite2 keeps IPv4 data and where the reference readers'
    # 96-zero-bit entry walk expects to find it.
    root = _Node()
    nodes: list[_Node] = [root]

    def child(of: _Node, bit: int) -> _Node:
        fresh = _Node()
        nodes.append(fresh)
        if bit == 0:
            of.left = fresh
        else:
            of.right = fresh
        return fresh

    for network, data_offset in encoded.items():
        net = ipaddress.ip_network(network)
        prefix_len = net.prefixlen
        max_prefix = net.max_prefixlen
        value = int(net.network_address)
        if ip_version == 6 and net.version == 4:
            # The 32-bit IPv4 value trails 96 zero bits unchanged;
            # only the prefix length and bit budget change.
            prefix_len = 96 + prefix_len
            max_prefix = 128
        current = root
        for bit_position in range(prefix_len):
            bit = (value >> (max_prefix - 1 - bit_position)) & 1
            if bit_position == prefix_len - 1:
                if bit == 0:
                    current.left = ("data", data_offset)
                else:
                    current.right = ("data", data_offset)
                break
            existing = current.left if bit == 0 else current.right
            if isinstance(existing, _Node):
                current = existing
            elif isinstance(existing, tuple):
                # A shorter network's leaf occupies the path this
                # longer one needs. A real writer would split the
                # leaf; a fixture does not need to -- it just refuses,
                # loudly, rather than writing a silently wrong file.
                raise ValueError("overlapping networks are not supported by the fixture writer")
            else:
                current = child(current, bit)

    node_count = len(nodes)
    tree_blob = b"".join(_serialize_node(node, nodes, node_count, record_size) for node in nodes)

    # Metadata with the exact declared types reference writers use:
    # uint16 fields as uint16, node_count as uint32, build_epoch as
    # uint64 -- the strict validators in reference readers check the
    # declared type, so the fixture declares the canonical ones.
    metadata = encode_map_raw(
        [
            ("binary_format_major_version", encode_uint_of_type(_TYPE_UINT16, 2)),
            ("binary_format_minor_version", encode_uint_of_type(_TYPE_UINT16, 0)),
            ("build_epoch", encode_uint_of_type(_TYPE_UINT64, build_epoch)),
            ("database_type", encode_utf8_string(database_type)),
            ("description", encode_map({"en": description})),
            ("ip_version", encode_uint_of_type(_TYPE_UINT16, ip_version)),
            ("languages", encode_array(["en"])),
            ("node_count", encode_uint_of_type(_TYPE_UINT32, node_count)),
            ("record_size", encode_uint_of_type(_TYPE_UINT16, record_size)),
        ]
    )
    return tree_blob + b"\x00" * _DATA_SECTION_SEPARATOR + data_blob + _METADATA_MARKER + metadata


def _record_value(slot: _Node | tuple[str, int] | None, nodes: list[_Node], node_count: int) -> int:
    """One record slot -> its in-file value. Data leaves encode as
    `node_count + 16 + data_offset` -- the 16-byte separator is baked
    into the leaf value, exactly as the reference writers do, so a
    record value of exactly `node_count` stays unambiguous as the
    empty record and offset 0 stays reachable. UNFILLED slots encode
    as `node_count` too -- the empty record every real writer emits
    and the one value the reference readers treat as not-found (they
    resolve a 0 record as "back to node 0", so a 0-filled tree reads
    as corrupt, not as empty)."""
    if isinstance(slot, _Node):
        return nodes.index(slot)
    if isinstance(slot, tuple):
        return node_count + _DATA_SECTION_SEPARATOR + slot[1]
    return node_count


def _serialize_node(node: _Node, nodes: list[_Node], node_count: int, record_size: int) -> bytes:
    """Node -> record_size-conformant bytes, byte-for-byte the
    reference format. At record_size 28 both records keep their LOW
    24 bits as plain 3-byte runs and both HIGH nibbles share one
    middle byte -- left's in its high nibble, right's in its low
    nibble (the spec's `| 23..0 | 27..24 | 27..24 | 23..0 |`)."""
    left = _record_value(node.left, nodes, node_count)
    right = _record_value(node.right, nodes, node_count)
    if record_size == 24:
        return left.to_bytes(3, "big") + right.to_bytes(3, "big")
    if record_size == 28:
        shared = (((left >> 24) & 0x0F) << 4) | ((right >> 24) & 0x0F)
        return (left & 0xFFFFFF).to_bytes(3, "big") + bytes([shared]) + (right & 0xFFFFFF).to_bytes(3, "big")
    return left.to_bytes(4, "big") + right.to_bytes(4, "big")
