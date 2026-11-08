"""NetScope TUI screens (v0.6): one Screen class per view, all sharing
the shell's state.

The single-screen dashboard from v0.2 outgrew one screen's worth of
information; Phase I of future-roadmap.md (TASK-041 -- TASK-046) always
planned a screen per concern. Each screen in this package is mounted
ONCE by the shell (`ui/tui/app.py`) and stays alive for the whole
session -- switching screens never discards a screen's state.

Every screen that needs live measurement data implements
`on_new_round(measurements, experience, diagnosis, transition)`, which
the shell calls both when a round lands and again when the screen
becomes active, so no screen ever shows stale data. Screens decide
nothing about what the numbers mean (the same rule ui/cli.py follows):
scoring, diagnosis and incident semantics live in `app`/`core`, and
screens only decide how results LOOK.
"""
