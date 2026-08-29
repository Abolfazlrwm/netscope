"""
netscope.probes.tls_probe

TLS handshake-timing measurement using Python's standard library only
(ssl, layered on tcp_probe's socket), per TASK-017's explicit
requirement. Like tcp_probe.py, this is a brand-new implementation with
no legacy behavior to preserve, so it classifies `error_type` directly
at the point each real exception is caught -- never string matching.

"Layered on TASK-016's socket" (future-roadmap.md's own phrasing for
this task) means: this probe reuses tcp_probe._open_connected_socket()
to establish the underlying TCP connection, rather than re-implementing
TCP connection logic here -- exactly the sharing that helper exists for.

DESIGN DECISION -- what "handshake timing" measures (documented here
since the roadmap doesn't specify this precisely): `latency_ms` on a
successful measurement represents the TLS handshake duration ALONE,
not TCP-connect-plus-handshake combined. TCP connection time is already
tcp_probe.py's own metric (TASK-016); giving this probe a separate,
handshake-only timing makes it a genuinely distinct diagnostic signal --
e.g. it lets a slow network path (TCP-layer symptom) be distinguished
from slow/expensive TLS negotiation (this probe's symptom) when
diagnosing a connection, rather than conflating the two into one number.
"""

from __future__ import annotations

import socket
import ssl
import time

from netscope.core.models import ProbeErrorType, ProbeType, RawMeasurement, utcnow
from netscope.probes import tcp_probe


def handshake(host: str, port: int, timeout: float = 2.0) -> RawMeasurement:
    """Establish a TCP connection to (host, port) via tcp_probe's shared
    connection helper, then perform a TLS handshake over it and time
    only the handshake portion, using time.perf_counter() (monotonic,
    matching tcp_probe.connect()'s own convention -- never datetime).

    The TLS/TCP socket is always closed before returning, whether the
    handshake succeeded or failed -- no persistent connection is ever
    left open, and no application data is sent after the handshake
    completes (this probe's responsibility ends at the handshake).
    """
    target = f"{host}:{port}"

    try:
        sock = tcp_probe._open_connected_socket(host, port, timeout)
    except socket.timeout:
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=False,
            error=f"TCP connection to {target} timed out before the TLS handshake could start",
            error_type=ProbeErrorType.TIMEOUT,
        )
    except ConnectionRefusedError as exc:
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=False,
            error=str(exc),
            error_type=ProbeErrorType.CONNECTION_REFUSED,
        )
    except PermissionError as exc:
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=False,
            error=str(exc),
            error_type=ProbeErrorType.PERMISSION_DENIED,
        )
    except OSError as exc:
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=False,
            error=str(exc),
            error_type=ProbeErrorType.UNKNOWN,
        )

    tls_sock: ssl.SSLSocket | None = None
    start = time.perf_counter()
    try:
        context = ssl.create_default_context()
        # server_hostname is required by ssl.create_default_context()'s
        # default check_hostname=True / verify_mode=CERT_REQUIRED for
        # certificate hostname verification to work at all.
        tls_sock = context.wrap_socket(sock, server_hostname=host)
        elapsed_ms = (time.perf_counter() - start) * 1000
        cipher = tls_sock.cipher()
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=True,
            latency_ms=elapsed_ms,
            extra={
                "tls_version": tls_sock.version(),
                "cipher": cipher[0] if cipher else None,
            },
        )
    except socket.timeout:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=f"TLS handshake with {target} timed out",
            error_type=ProbeErrorType.TIMEOUT,
        )
    except ssl.SSLError as exc:
        # Covers both generic SSLError and its subclass
        # SSLCertVerificationError (certificate hostname/chain/expiry
        # failures) -- both are TLS-specific negotiation failures, kept
        # in one TLS_FAILURE bucket rather than splitting into a second
        # new enum value not required by this task.
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.TLS_FAILURE,
        )
    except OSError as exc:
        # Defensive fallback mirroring _open_connected_socket's own
        # generic OSError handling: ssl.SSLError is itself an OSError
        # subclass and is already caught above, but wrap_socket() can
        # in principle raise a plain OSError not specific to TLS
        # negotiation (e.g. the underlying socket dropping mid-handshake).
        # Classified as UNKNOWN rather than TLS_FAILURE, since it isn't
        # actually a TLS-specific failure.
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RawMeasurement(
            probe_type=ProbeType.TLS,
            target=target,
            timestamp=utcnow(),
            success=False,
            latency_ms=elapsed_ms,
            error=str(exc),
            error_type=ProbeErrorType.UNKNOWN,
        )
    finally:
        if tls_sock is not None:
            tls_sock.close()
        else:
            sock.close()
