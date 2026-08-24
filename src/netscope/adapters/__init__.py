"""
netscope.adapters

Everything NetScope talks to the outside world (network, OS) through
lives here: probe implementations wrapping third-party libraries
(icmplib, dnspython, httpx, stdlib socket/ssl), and OS-level discovery
(gateway, interfaces).

This package is a placeholder for now. Per docs/architecture/adr-002-
probe-adapter-strategy.md and future-roadmap.md, existing probes
(src/netscope/probes/) and any discovery logic are relocated here in
later, separately-scoped tasks (TASK-014 through TASK-018 for probes,
TASK-010 through TASK-012 for discovery) -- not in TASK-005.

Rule this package must always follow: it may depend on core (to
construct/return core.models types) and third-party libraries, but it
must never be depended on by core, and it must never contain diagnosis-
shaped judgments -- it only measures and returns.
"""
