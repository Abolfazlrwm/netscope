"""
netscope.adapters.lookups

ASN lookup adapter -- TASK-022, "ASN/ISP intelligence".

CORRECTION NOTE: this module originally wrapped the third-party `pyasn`
package. That implementation was rejected after the user's real
installation attempt on Windows + Python 3.13 (Miniconda, x64) failed:

    building 'pyasn.pyasn_radix' extension
    error: Microsoft Visual C++ 14.0 or greater is required.

This is not a one-off environment quirk -- pyasn ships no prebuilt
Windows wheel on PyPI (confirmed via a long-standing, still-open
upstream GitHub issue asking for one: hadiasghari/pyasn issue #57,
"wheel available somewhere?"), and its own Arch Linux package listing
shows a compiled `pyasn_radix*.so` extension built with `make`, i.e.
pyasn requires native compilation on any platform without a
distro-provided prebuilt binary. Requiring Microsoft C++ Build Tools
for a normal `pip install -e .` is not acceptable for a project meant
to install cleanly cross-platform.

REPLACEMENT: a small, pure-Python longest-prefix-match lookup using
only the standard library's `ipaddress` module (available since Python
3.3, no version-specific concerns for 3.13) -- zero third-party
dependencies, zero native compilation, identical behavior on Windows,
Linux, and macOS. This is "option C" from the correction task's own
candidate list (a small internal parser/adapter for a documented local
database format), chosen over every third-party alternative (pytricia,
radix, py-radix all considered and rejected) specifically because each
of those has the same C-extension-compilation profile as pyasn --
switching to any of them would not have actually solved the problem,
only relocated it.

DATABASE FORMAT (NetScope's own -- not a byte-for-byte guarantee of
compatibility with arbitrary real-world pyasn-generated database
files, which were not independently inspected): one entry per line,

    NETWORK/PREFIXLEN<whitespace>ASN

e.g. "1.1.1.0/24    13335". Blank lines and lines starting with ';' or
'#' are treated as comments and skipped. This mirrors the simple
"prefix, whitespace, ASN" convention the original pyasn library's own
`ipasn_string` parameter accepted for straightforward entries (verified
directly against that library before it was removed from this
project), so simple existing database text remains portable to this
format.

No network access happens anywhere in this module. The database (a
file path, or an in-memory string -- e.g. a small fixture in tests) is
supplied by the caller at construction time; this module does not
fetch, generate, or download it.

SCOPE NOTE -- "ISP" half of the task title: this module looks up ASN
numbers and their matching prefix only. Organization/ISP name
resolution is not implemented here -- there is no reliable, license-
clean, dependency-free data source for it in this task's scope (the
original pyasn-based implementation could not have reliably provided
this either: pyasn.get_as_name() was itself explicitly documented by
that library as "Under construction, do not use!").
core.models.RouteHop.organization remains unpopulated by this adapter.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional


class AsnLookupUnavailableError(RuntimeError):
    """Raised when the supplied database cannot be read/loaded/parsed
    at all. A defined, explicit failure mode, distinct from a normal
    "no match" lookup result -- callers can tell "the lookup capability
    itself isn't usable" apart from "this particular address just
    isn't in the loaded table," which is not an error (see
    AsnLookupResult's own docstring)."""


@dataclass
class AsnLookupResult:
    """Result of an ASN lookup for one IP address.

    asn/prefix are both None when the address has no match in the
    loaded database (e.g. private/reserved address space, or an
    address simply not present in the loaded table) -- this is a
    normal, expected outcome, not an error condition."""

    ip_address: str
    asn: Optional[int]
    prefix: Optional[str]

    @property
    def matched(self) -> bool:
        return self.asn is not None


def _parse_database(text: str) -> dict[int, dict[int, tuple[int, str]]]:
    """Parse NetScope's simple ASN database text format (see this
    module's own docstring for the exact format) into a lookup table
    bucketed by prefix length: {prefixlen: {network_address_int:
    (asn, prefix_str)}}.

    Raises ValueError on a malformed data line -- caught and wrapped by
    AsnLookupAdapter's constructor into AsnLookupUnavailableError.
    """
    by_prefixlen: dict[int, dict[int, tuple[int, str]]] = {}

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"malformed database line {line_number}: {raw_line!r}")

        prefix_text, asn_text = parts
        try:
            network = ipaddress.ip_network(prefix_text, strict=False)
            asn = int(asn_text)
        except ValueError as exc:
            raise ValueError(f"malformed database line {line_number}: {raw_line!r}") from exc

        bucket = by_prefixlen.setdefault(network.prefixlen, {})
        bucket[int(network.network_address)] = (asn, str(network))

    return by_prefixlen


def _longest_prefix_match(
    by_prefixlen: dict[int, dict[int, tuple[int, str]]], ip_address: str
) -> tuple[Optional[int], Optional[str]]:
    """Pure-Python longest-prefix-match: checks candidate networks from
    the most specific prefix length down to the least specific (0,
    the default route), returning the first (most specific) match.

    Raises ValueError if `ip_address` is not a syntactically valid IP
    address -- deliberately not caught here, propagated to the caller,
    since invalid input is a caller programming error distinct from a
    legitimate-but-unmatched address (matches the behavior verified
    against the original pyasn library before it was removed).
    """
    addr = ipaddress.ip_address(ip_address)
    addr_int = int(addr)
    max_prefixlen = addr.max_prefixlen  # 32 for IPv4, 128 for IPv6
    full_mask = (1 << max_prefixlen) - 1

    for prefixlen in range(max_prefixlen, -1, -1):
        bucket = by_prefixlen.get(prefixlen)
        if not bucket:
            continue
        shift = max_prefixlen - prefixlen
        mask = full_mask ^ ((1 << shift) - 1) if shift else full_mask
        candidate_network_int = addr_int & mask
        match = bucket.get(candidate_network_int)
        if match is not None:
            return match

    return None, None


class AsnLookupAdapter:
    """Looks up the ASN and matching prefix for an IP address using a
    local, pure-Python-parsed ASN database -- see this module's own
    docstring for the exact format and for why this replaced the
    original pyasn-based implementation.

    Exactly one of `ipasn_file` (a path to an existing database file)
    or `ipasn_string` (an in-memory database, e.g. a small fixture in
    tests) must be supplied -- these parameter names are kept
    unchanged from the original implementation for API compatibility,
    even though they no longer name a pyasn-specific concept. Loading
    happens once, at construction: if the database can't be
    read/parsed, this raises AsnLookupUnavailableError immediately
    rather than deferring the failure to the first lookup() call,
    since an adapter with no usable database can never succeed at any
    lookup anyway.
    """

    def __init__(self, ipasn_file: Optional[str] = None, ipasn_string: Optional[str] = None) -> None:
        if ipasn_file is None and ipasn_string is None:
            raise ValueError("either ipasn_file or ipasn_string must be provided")

        if ipasn_string is not None:
            text = ipasn_string
        else:
            try:
                with open(ipasn_file, encoding="utf-8") as f:
                    text = f.read()
            except OSError as exc:
                raise AsnLookupUnavailableError(f"failed to read ASN database file: {exc}") from exc

        try:
            self._by_prefixlen = _parse_database(text)
        except ValueError as exc:
            raise AsnLookupUnavailableError(f"failed to parse ASN database: {exc}") from exc

    def lookup(self, ip_address: str) -> AsnLookupResult:
        """Look up `ip_address` against the loaded database.

        Returns an AsnLookupResult with asn=None/prefix=None if the
        address has no match -- a normal outcome, not an error.

        Raises ValueError if `ip_address` is not a syntactically valid
        IP address string -- a caller programming error, distinct from
        a legitimate-but-unmatched address.
        """
        asn, prefix = _longest_prefix_match(self._by_prefixlen, ip_address)
        return AsnLookupResult(ip_address=ip_address, asn=asn, prefix=prefix)
