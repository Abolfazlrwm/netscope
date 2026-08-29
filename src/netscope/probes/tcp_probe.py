"""
netscope.probes.tcp_probe

TCP connect-timing measurement using Python's standard library only
(socket) -- no third-party library, per TASK-016's explicit requirement.
Measures TCP connection establishment time, nothing more: this probe
never sends application-level data after the handshake completes, and
always closes the socket immediately once timing is captured, whether
the attempt succeeded or failed.

Unlike icmp_probe.py/dns_probe.py (which predate structured errors and
only ever emit a free-text `error: str`, requiring their adapters to
classify errors from that string after the fact), this is a brand-new
implementation with no legacy behavior to preserve. It classifies
`error_type` directly at the point each real exception is caught, using
the actual exception types Python's socket module raises -- never
string matching. See adapters/probes/tcp_adapter.py for why the
resulting adapter is a pure pass-through with no classification logic
of its own.
"""

from __future__ import annotations

import socket
import time

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement, utcnow


def _open_connected_socket(host: str, port: int, timeout: float) -> socket.socket:
    """Create a TCP socket, set its timeout, and connect it to (host, port).

    Returns the connected, still-open socket -- the caller is
    responsible for closing it. On failure, closes the partially-created
    socket itself before re-raising: if socket.connect() fails, the
    exception propagates out of this function before the caller's
    `sock = _open_connected_socket(...)` assignment ever completes, so
    the caller never gets a reference to close -- this function must
    guarantee no leak on its own, not rely on the caller's finally block
    to reach a socket it can never see.

    Raises whatever socket.socket()/settimeout()/connect() raise; does
    no further exception handling/classification of its own, so
    connect() (below) and tls_probe.py's handshake() (TASK-017,
    "layered on TASK-016's socket" per future-roadmap.md) can share
    identical connection-establishment behavior, each classifying
    failures in its own try/except.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
    except Exception:
        sock.close()
        raise
    return sock


def connect(host: str, port: int, timeout: float = 2.0) -> RawMeasurement:
    """Attempt a TCP connection to (host, port) and time how long
    connection establishment takes.

    Uses time.perf_counter() -- a monotonic clock -- for the elapsed
    duration, never datetime (wall-clock time is unsuitable for
    measuring elapsed duration: it can jump backward or forward due to
    NTP adjustments, DST changes, or manual clock changes, which
    time.perf_counter() is immune to).

    The socket is always closed in a `finally` block, whether the
    connection succeeded or failed -- no persistent connection is ever
    left open, and no data is sent after the handshake completes.
    """
    target = f"{host}:{port}"
    start = time.perf_counter()
    sock: socket.socket | None = None
    try:
        sock = _open_connected_socket(host, port, timeout)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TCP,
            target=target,
            timestamp=utcnow(),
            success=True,
            latency_ms=elapsed_ms,
        )
    except socket.timeout:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TCP,
            target=target,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=f"Connection attempt to {target} timed out after {timeout}s",
            error_type=ProbeErrorType.TIMEOUT,
        )
    except ConnectionRefusedError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TCP,
            target=target,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.CONNECTION_REFUSED,
        )
    except PermissionError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TCP,
            target=target,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.PERMISSION_DENIED,
        )
    except OSError as exc:
        # Covers everything else socket.connect() can raise (e.g.
        # "Network is unreachable", DNS resolution failure of `host`
        # itself, etc.). These are intentionally NOT split into finer
        # categories (e.g. a dedicated NETWORK_UNREACHABLE) for this
        # task -- TASK-016 only adds CONNECTION_REFUSED, the one
        # category with a distinct, cleanly-typed builtin exception
        # that's common enough to justify its own bucket. Splitting
        # generic OSError further would need errno inspection for a
        # need not yet concretely justified; UNKNOWN is the deliberate,
        # honest classification for "failed, and not one of the cases
        # above" rather than a guess.
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TCP,
            target=target,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.UNKNOWN,
        )
    finally:
        if sock is not None:
            sock.close()
