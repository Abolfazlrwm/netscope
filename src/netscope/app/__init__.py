"""
netscope.app

Application orchestration / composition root.

Per docs/architecture/architecture-overview.md §2-3 and adr-001-
architecture-style.md, this is the one package responsible for wiring
concrete adapters and persistence implementations to core's ports, and
exposing use cases (e.g. run_measurement_round(), diagnose_now()) as
the only API the ui package is allowed to call.

This was the boundary the implementation audit found missing entirely:
today's src/netscope/ui/cli.py does this job by accident, importing
probes/, intelligence/, diagnosis/, explanation/, and persistence/
directly with no orchestration layer between them.

This package is a placeholder for now. No use cases are implemented in
TASK-005 -- that begins once the underlying pieces they'd orchestrate
(probes, baseline persistence, diagnosis) have themselves been
relocated/rewritten in their own later, separately-scoped tasks. Moving
CLI orchestration here, or implementing a real application service, is
explicitly out of scope for this task.
"""
