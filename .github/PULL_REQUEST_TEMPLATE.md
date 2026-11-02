## What does this PR change?

<!-- One or two sentences: the user-visible or architectural change. -->

## Why is it needed?

<!-- Link the issue, or describe the motivation. Reference the relevant ADR
     in docs/architecture/ if this touches layer boundaries. -->

## How was it tested?

- [ ] `make test` passes locally (the suite is fully offline)
- [ ] `make lint` and `make typecheck` are clean
- [ ] New behavior is covered by new tests

## Architecture checklist (hexagonal rules)

- [ ] `netscope.core` still imports **only the standard library**
- [ ] New adapters implement a port from `core.ports`; no adapter imports another adapter
- [ ] New dependencies are justified (stdlib-first policy) and noted in `NOTICE` if licensing differs
- [ ] `CHANGELOG.md` updated for user-visible changes
