"""
Minimal CLI (MVP UI). Runs one full cycle:
probes -> experience score -> diagnosis -> human explanation -> save to SQLite.

This is intentionally not the Textual TUI yet -- it exists to prove the
architecture end-to-end with a working, runnable command.
"""

from __future__ import annotations

import argparse

from netscope.core.baseline import UserBaseline
from netscope.core.diagnosis import diagnose
from netscope.core.models import Evidence, RawMeasurement, Severity
from netscope.core.scoring import score_measurements
from netscope.explanation.explainer import explain
from netscope.persistence.sqlite_store import SqliteStore
from netscope.probes import dns_probe, http_probe, icmp_probe

PUBLIC_DNS = "1.1.1.1"
PUBLIC_CDN_HTTP = "https://www.cloudflare.com/"


def _to_evidence(metric: str, measurement: RawMeasurement | None) -> Evidence:
    """Minimal glue turning this CLI's raw probe results into Evidence so
    core/diagnosis.py's evidence-only diagnose() can be called at all.
    This is NOT the "evidence generation" TASK-027 owns building
    properly (real baseline-relative deviation/confidence) -- it is
    deliberately the smallest possible bridge to keep the CLI runnable
    now that diagnose()'s signature changed to accept Evidence instead
    of raw measurements directly, per TASK-026's "minimal call-site
    changes, no CLI architecture redesign" scope.
    """
    if measurement is None:
        return Evidence(metric=metric, tested=False)
    severity = Severity.CRITICAL if not measurement.success else Severity.INFO
    return Evidence(
        metric=metric,
        tested=True,
        observed_value=measurement.latency_ms,
        severity=severity,
        source=measurement,
    )


def run_once(gateway: str | None = None) -> None:
    store = SqliteStore()

    local_gateway = icmp_probe.ping(gateway) if gateway else None
    public_dns = icmp_probe.ping(PUBLIC_DNS)
    dns_lookup = dns_probe.resolve("example.com")
    public_cdn = http_probe.fetch(PUBLIC_CDN_HTTP)

    measurements = [m for m in [local_gateway, public_dns, dns_lookup, public_cdn] if m]
    for m in measurements:
        store.save(m)

    # NOTE: no BaselineRepository-backed load/persist wiring exists yet
    # (that's app/use_cases.py's job per architecture-overview.md SS10,
    # not yet built) -- this CLI already didn't persist a baseline
    # across runs before TASK-025, so a fresh, empty UserBaseline() here
    # preserves that exact status quo rather than adding new
    # orchestration. It exists solely to satisfy scoring's new required
    # `baseline` parameter.
    baseline = UserBaseline()
    experience = score_measurements(measurements, baseline)
    print(f"\nExperience score: {experience.score}/100 ({experience.level.value})\n")

    evidence = [
        _to_evidence("gateway_latency", local_gateway),
        _to_evidence("dns_latency", public_dns),
        _to_evidence("destination_latency", public_cdn),
    ]
    diagnosis = diagnose(evidence)
    if diagnosis is None:
        print("No issues detected.")
    else:
        print(explain(diagnosis))

    store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="netscope", description="NetScope network diagnostics")
    parser.add_argument(
        "--gateway",
        help="IP of your local router/gateway, to localize local vs. upstream issues",
        default=None,
    )
    args = parser.parse_args()
    run_once(gateway=args.gateway)


if __name__ == "__main__":
    main()
