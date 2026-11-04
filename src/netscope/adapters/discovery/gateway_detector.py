"""
netscope.adapters.discovery.gateway_detector

Default-gateway detection adapter -- best-effort, cross-platform.

WHY THIS EXISTS
---------------
`netscope diagnose --gateway` localizes problems (local Wi-Fi/router vs.
ISP vs. destination), but until now the user had to know and type their
router's IP on every run. Most users don't. This adapter answers the
"what's my default gateway?" question automatically so the CLI can ask
it by default (see `ui/cli.py`'s `--gateway auto` handling and
app/gateway.py's auto-resolution policy).

PLATFORM STRATEGIES (first success wins, then cached)
------------------------------------------------------
- Linux: parse `/proc/net/route` -- the row whose destination column is
  00000000 carries the gateway as a little-endian hex address. Pure
  file read, no subprocess, no permissions needed.
- macOS: `route -n get default` -- parse the `gateway:` line.
- Windows: `route print 0.0.0.0` -- parse the `0.0.0.0  0.0.0.0
  <gateway>  ...` active-route row. The regex tolerates the variable
  whitespace/columns Windows uses across versions/locales.

CONTRACT
--------
- Never raises: any failure (unknown platform, missing tool, parse
  mismatch, timeout) returns None -- gateway detection is an
  optimization, and the diagnose pipeline already handles "no gateway
  measured" honestly (Hypothesis.INSUFFICIENT_EVIDENCE rather than a
  fabricated healthy local result).
- No hostname resolution, no network I/O to the gateway itself -- pure
  local OS introspection. ICMP-probing the gateway remains the probe
  layer's job.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

_PROC_NET_ROUTE = Path("/proc/net/route")


def _detect_linux() -> str | None:
    """Linux: read the default route from /proc/net/route."""
    try:
        lines = _PROC_NET_ROUTE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines[1:]:  # first line is the header
        parts = line.split()
        if len(parts) < 3:
            continue
        _iface, destination, gateway = parts[0], parts[1], parts[2]
        if destination != "00000000" or gateway == "00000000":
            continue
        try:
            packed = bytes.fromhex(gateway)
            # /proc/net/route stores the address little-endian
            addr = ".".join(str(b) for b in reversed(packed))
        except ValueError:
            continue
        if addr.count(".") == 3 and not addr.startswith("0.0.0.0"):
            return addr
    return None


def _detect_macos() -> str | None:
    """macOS: parse `route -n get default`."""
    if shutil.which("route") is None:
        return None
    try:
        out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"^\s*gateway:\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$", out, re.MULTILINE)
    return match.group(1) if match else None


def _detect_windows() -> str | None:
    """Windows: parse the active default route from `route print`."""
    if shutil.which("route") is None:
        return None
    try:
        out = subprocess.run(
            ["route", "print", "0.0.0.0"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    # The active-route table row: destination 0.0.0.0, netmask 0.0.0.0, then gateway.
    for line in out.splitlines():
        if "0.0.0.0" not in line:
            continue
        match = re.search(
            r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\d{1,3}(?:\.\d{1,3}){3})",
            line,
        )
        if match:
            return match.group(1)
    return None


class GatewayDetector:
    """Detects the local default gateway, best-effort, per platform.

    `strategy` is injectable for tests (e.g. a lambda returning a fixed
    address); production code uses `detect_default_gateway()`, which
    picks the right platform strategy automatically.
    """

    def __init__(self, strategy=None):
        self._strategy = strategy

    def detect(self) -> str | None:
        if self._strategy is not None:
            return self._strategy()
        return detect_default_gateway()


def detect_default_gateway() -> str | None:
    """Best-effort default gateway address, or None if it can't be
    determined. Never raises."""
    if sys.platform.startswith("linux"):
        return _detect_linux()
    if sys.platform == "darwin":
        return _detect_macos()
    if sys.platform == "win32":
        return _detect_windows()
    return None
