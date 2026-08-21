# ADR-004 — UI Framework

**Status:** Accepted

## Context

Phase 1 research (§4) already proposed Textual for the MVP and left PySide6 or
Tauri as later options. `pyproject.toml` already declares `textual` as an
optional `ui` extra (unused so far — no `ui/tui/` code exists yet, confirmed
in the TASK-002 inventory). This ADR re-evaluates that choice with the
architecture in §2–§3 in mind (UI depends only on `app`) rather than treating
it as settled by default.

## Options evaluated

| | **Textual** | PySide6 | Tauri |
|---|---|---|---|
| License | MIT | LGPL-3.0 (free/dynamic use; commercial license also available) | MIT + Apache-2.0 (dual) |
| Language | Python (same as `core`/`app`) | Python bindings over Qt (C++) | Rust backend + web frontend (HTML/CSS/JS) |
| Cross-platform | Yes, terminal-based — works identically over SSH, in a plain terminal, in CI logs | Yes, native windowing per OS | Yes, native webview per OS |
| Packaging | Trivial — it's a Python dependency like any other; ships inside the same `pip install` as `core`/`app` | Heavier — Qt runtime bundling, larger installers | Requires a Rust toolchain in the build pipeline and a second language/runtime alongside the Python core |
| Performance | Fine for a diagnostics dashboard's update rate (sub-second refresh, not high-FPS rendering) | Better for graphics-heavy UI, unnecessary here | Fine, webview overhead is negligible for this use case |
| Maintainability | **Single language across `core`/`app`/`ui`** — a contributor working on diagnosis logic can read and modify the UI without switching ecosystems | Adds Qt/C++-adjacent concepts (signals/slots, widget trees) on top of Python | Adds a second language (Rust) and a second dependency ecosystem (npm) to a project that is otherwise pure Python |
| Fit for MVP | Very good — a diagnostics tool is naturally table/log/status-shaped, which is Textual's strength | Overkill for the MVP's actual UI needs (no need for native graphics, drag-and-drop, etc.) | Overkill for the same reason, plus adds build-pipeline complexity disproportionate to the MVP |

## Decision

**Textual for the MVP UI**, confirming Phase 1's choice, now justified against
this task's architecture specifically: because `ui` in §3's dependency table
is only allowed to depend on `app` (use cases) and `core` (models, for typing/
display), the UI framework choice has almost no bearing on `core`/`adapters`/
`persistence` at all — this is precisely what makes swapping it out later
("Can the UI be replaced without rewriting the network engine?", answered yes
in architecture-overview.md §16) actually cheap regardless of which framework
is picked today. Textual is chosen over the alternatives specifically because
it keeps the whole stack in one language during the phase where `core` and
`app` are still being actively designed — not because PySide6/Tauri are
disqualified in principle.

**PySide6 or Tauri remain viable later** if NetScope needs native
graphics/animation (PySide6) or wants a more visually polished cross-platform
desktop app with a web-tech frontend (Tauri) once the domain/application layer
has stabilized — that migration only touches `ui/`, per this architecture.

## Consequences

- `pyproject.toml`'s existing `ui` optional-dependency group (`textual`) is
  confirmed correct and needs no change.
- `ui/` will depend on `app`'s use cases exclusively; no `adapters` or
  `persistence` imports are permitted inside `ui/`, enforced by the dependency
  table in architecture-overview.md §3 (not by tooling yet — that's a future
  task, e.g. an import-linter rule).
