"""
netscope.adapters.discovery.network_type_classifier

Best-effort classification of a network interface's connection type
(Wi-Fi, Ethernet, or Cellular) from its interface name alone.

WHY THIS IS BEST-EFFORT, NOT RELIABLE
----------------------------------------
There is no single, universally reliable way to determine an
interface's physical connection type from userspace across Windows,
Linux, and macOS without OS-specific APIs this project's dependency
policy doesn't currently justify adding (dependency-strategy.md). What
IS reasonably consistent, though never guaranteed, is naming
convention:

- Linux's systemd predictable network interface naming scheme uses
  "wl*" for wireless LAN (wlan0, wlp2s0), "ww*" for WWAN/cellular
  modems (wwan0, wwp0s20u), and "en*" for Ethernet (enp0s3, eno1,
  ens33), alongside the older, still-common "eth*"/"wlan*" naming.
- Windows and macOS commonly expose human-readable adapter names
  ("Wi-Fi", "Ethernet", "Cellular") via the same libraries/APIs this
  project already uses (psutil), rather than only raw device names.

This module matches against both conventions, case-insensitively.
Any interface name that doesn't match a known pattern -- including
loopback ("lo"), virtual/container interfaces (Docker's "docker0",
"veth*"), bridges, tunnels ("tun*"/"tap*"), and simply unrecognized
names -- classifies as NetworkType.UNKNOWN. This is a deliberate,
safe default, not a failure: TASK-012 explicitly does not introduce a
fourth physical network type to avoid this uncertainty, and callers
must not treat UNKNOWN as an error.

This classification never depends on having an active connection, DNS
resolution, or any network I/O -- it is a pure, deterministic function
of the interface name string, kept isolated here specifically so it is
easy to unit test without psutil or any other adapter dependency.
"""

from __future__ import annotations

from netscope.core.models import NetworkType

# Checked in this order deliberately: cellular's "ww" prefix is checked
# before wifi's "wl" prefix (both start with "w", but "ww" is more
# specific and must win), and ethernet's "en"/"eth" prefixes are
# checked last since they're the least likely to collide with the
# others.
_CELLULAR_MARKERS = ("wwan", "cellular", "mobile", "rmnet", "ppp")
_WIFI_MARKERS = ("wifi", "wi-fi", "wireless", "airport")
_ETHERNET_MARKERS = ("ethernet",)


def classify_network_type(interface_name: str | None) -> NetworkType:
    """Best-effort classification of `interface_name` into Wi-Fi,
    Ethernet, Cellular, or Unknown. Never raises -- missing, empty, or
    unrecognized input all safely classify as NetworkType.UNKNOWN."""

    if not interface_name:
        return NetworkType.UNKNOWN

    name = interface_name.strip().lower()
    if not name:
        return NetworkType.UNKNOWN

    if name.startswith("ww") or any(marker in name for marker in _CELLULAR_MARKERS):
        return NetworkType.CELLULAR

    if name.startswith("wl") or any(marker in name for marker in _WIFI_MARKERS):
        return NetworkType.WIFI

    if name.startswith(("en", "eth")) or any(marker in name for marker in _ETHERNET_MARKERS):
        return NetworkType.ETHERNET

    return NetworkType.UNKNOWN
