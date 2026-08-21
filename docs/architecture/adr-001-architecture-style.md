# ADR-001 — Architecture Style: Ports-and-Adapters over Fine-Grained Package Splitting

**Status:** Accepted
**Context documents:** `NetScope-Research-Phase1.md` §5, `docs/architecture/implementation-audit.md`

## Context

Phase 1 research proposed 11 top-level packages under `src/netscope/`:
`core/probes/routing/intelligence/diagnosis/monitoring/explanation/reporting/
persistence/infrastructure/ui`. The MVP built against a variant of this. The
implementation audit (TASK-002) then found:

- Five of those packages (`routing`, `monitoring`, `reporting`,
  `intelligence/services`, and effectively `infrastructure`) stayed empty
  through the entire MVP build.
- `intelligence/baseline.py` was fully implemented but never wired into
  `experience_score.py` or `diagnosis/engine.py`, because **no package was
  responsible for orchestration** — `ui/cli.py` absorbed that role by default,
  directly importing five different packages.
- `diagnosis/engine.py` and `core/models.Incident` became two competing models
  for the same concept, because "the shape of a diagnosed problem" wasn't
  clearly owned by either `diagnosis/` or `core/`.

## Decision

Consolidate to five top-level packages — `core`, `adapters`, `persistence`,
`app`, `ui` — following a ports-and-adapters (hexagonal-lite) style:

- `core` owns **all** domain models and **all** pure logic (baseline, scoring,
  diagnosis, explanation, route-churn, incidents), plus the `Protocol`
  definitions (`ports`) that adapters/persistence must satisfy.
- `adapters` owns **all** I/O that talks to the network or OS.
- `persistence` owns **all** I/O that talks to storage.
- `app` is the composition root and the only orchestration layer — this
  directly fills the gap the audit found.
- `ui` depends on `app` only.

This does not abandon Phase 1's intent (small, single-responsibility, pure
domain logic separated from I/O) — it changes *where the seams are*. Phase 1
split by *conceptual topic* (baseline vs. diagnosis vs. explanation vs.
monitoring, each its own package). This ADR splits by *dependency direction*
(pure vs. I/O vs. orchestration vs. presentation) instead, because that is the
seam the audit's actual bugs (orphaned baseline, model duplication, untestable
orchestration) all crossed.

## Why this doesn't contradict the research, and why it's a change anyway

Per the task instructions, this is stated explicitly rather than silently
overridden: **the research wasn't wrong given what was known before code
existed.** Fine-grained topic packages are a reasonable starting guess. What
changed is that the audit produced *evidence* — an orphaned module, a
duplicated model, an untestable orchestration layer — that a topic-based split
doesn't prevent, and a dependency-direction-based split does prevent by
construction (an orchestration bug is structurally impossible to hide when
there's exactly one package whose job is orchestration).

## Alternatives considered

- **Keep the Phase 1 11-package layout, just fill in the empty ones.** Rejected:
  this doesn't address the actual bug (missing orchestration boundary), it just
  adds more files to a structure that already produced one. The empty packages
  are a symptom, not the disease.
- **Full Clean Architecture / DDD (`domain/application/infrastructure/adapters`
  each with their own subpackages).** Rejected as ceremony for this project's
  current size — one dev-facing MVP, single-process, single-user. Revisit only
  if `core` or `adapters` individually grow large enough that a single file per
  concern stops being enough (i.e., let pain drive the next split, as this ADR
  itself is doing relative to Phase 1).
- **Flat, single-package script style (no `core`/`adapters` split at all).**
  Rejected: this is exactly what produced the untestable, tightly-coupled
  `ui/cli.py` the audit flagged — collapsing the *one* boundary (pure vs. I/O)
  that TASK-003 already proved has real value (51 deterministic, offline tests
  against the pure components, and zero against anything touching I/O).

## Consequences

- Adding a new probe touches `adapters` + `app`'s wiring only, never `core`.
- `core` remains 100%-offline-testable, as already demonstrated by TASK-003.
- The migration from today's package layout to this one is itself future work
  (see architecture-overview.md §15) and is not performed by this ADR.
