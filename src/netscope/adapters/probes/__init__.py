"""
netscope.adapters.probes

Thin adapter classes that make the existing, unmodified probe modules
(netscope.probes.icmp_probe, dns_probe, http_probe) satisfy
netscope.core.ports.Probe, per adr-008-probe-adapter-implementation.md.

These adapters contain no measurement logic of their own -- they
delegate every call to the existing module-level function and return
whatever it returns, unchanged.
"""
