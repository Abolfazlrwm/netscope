"""
Minimal CLI (MVP UI). Runs one full cycle:
probes -> experience score -> diagnosis -> human explanation -> save to SQLite.

This is intentionally not the Textual TUI yet -- it exists to prove the
architecture end-to-end with a working, runnable command.
"""

from __future__ import annotations

import argparse

from netscope.diagnosis.engine import diagnose
from netscope.explanation.explainer import explain
from netscope.intelligence.experience_score import score_measurements
from netscope.persistence.sqlite_store import SqliteStore
from netscope.probes import dns_probe, http_probe, icmp_probe

PUBLIC_DNS = "1.1.1.1"
PUBLIC_CDN_HTTP = "https://www.cloudflare.com/"


def run_once(gateway: str | None = None) -> None:
    store = SqliteStore()

    local_gateway = icmp_probe.ping(gateway) if gateway else None
    public_dns = icmp_probe.ping(PUBLIC_DNS)
    dns_lookup = dns_probe.resolve("example.com")
    public_cdn = http_probe.fetch(PUBLIC_CDN_HTTP)

    measurements = [m for m in [local_gateway, public_dns, dns_lookup, public_cdn] if m]
    for m in measurements:
        store.save(m)

    experience = score_measurements(measurements)
    print(f"\nExperience score: {experience.score}/100 ({experience.level.value})\n")

    diagnosis = diagnose(local_gateway=local_gateway, public_dns=public_dns, public_cdn=public_cdn)
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
