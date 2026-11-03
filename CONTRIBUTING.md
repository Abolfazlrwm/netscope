# Contributing to NetScope

Thanks for your interest in improving NetScope! This document covers the
project's non-obvious rules — the architecture is deliberate, and
contributions are reviewed against it.

## Development setup

```bash
git clone https://github.com/your-username/netscope
cd netscope
make install     # or: pip install -e ".[tui,dev]"
make test        # pytest — fully offline, should take ~2 seconds
make lint        # ruff check + format check
make typecheck   # mypy
```

The test suite is **fully offline**: every probe is a scripted fake.
CI runs the same suite on Linux/macOS/Windows across Python 3.11–3.13,
so please don't add tests that require real network access.

## The architecture rules (enforced by tests)

NetScope is hexagonal. These invariants are checked by AST-based tests
in `tests/`, so a violation fails CI with a test failure:

1. **`core` imports only the standard library and `core`.** No third
   party, no I/O, no wall-clock reads in domain logic. Everything in
   `core/` is testable without any network or filesystem.
2. **`app` is the only layer allowed to import a core port and a
   concrete adapter together** (the composition root lives there).
   `core` must never import `adapters`/`persistence`; `ui` must never
   import `adapters`/`persistence` directly — it goes through `app`.
3. **"Missing" is never "healthy".** An unprobed target produces
   `tested=False` evidence → `Hypothesis.INSUFFICIENT_EVIDENCE`, never a
   fabricated healthy verdict. This principle applies at every layer
   (scoring confidence, evidence, incident lifecycle).
4. **No second pipeline.** The CLI, TUI, and monitor all run the same
   `app.use_cases.run_measurement_round` / `run_route_analysis` use
   cases. Don't add a parallel orchestration path.
5. **One canonical schema.** All DDL lives in
   `persistence/schema.py`; repositories never hand-write table
   definitions.

## Pull requests

- Keep the change scoped: one behavior per PR, with tests.
- If you add a user-facing command/flag, cover it in `tests/test_cli.py`
  and (for read commands) give it a `--json` shape in `app/reports.py`.
- If you add a probe, wire it through `ProbeRegistry` and reuse the
  evidence/diagnosis pipeline end-to-end.
- Update `CHANGELOG.md` under *Unreleased*.
- Docstrings in this codebase explain **why**, not what — when you make
  a non-obvious decision, leave a note for the next reader.

## Commit style

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`,
`test:`, `chore:`) keep the changelog readable.

## Reporting bugs

Run the same command with `--verbose` and include: OS + Python version,
the command, and the JSON output (`--json`) where applicable. Never
paste your `~/.netscope/` database — it contains nothing sensitive, but
there's no need.
