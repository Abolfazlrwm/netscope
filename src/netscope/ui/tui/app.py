"""
netscope.ui.tui.app

NetScope's terminal dashboard shell (Textual) — `netscope tui`.

WHAT THE SHELL OWNS
-------------------
Since v0.6 the TUI is six screens under one shell (future-roadmap.md
Phase I, TASK-041 -- TASK-046). The shell owns exactly the things every
screen shares, and nothing else:

- dependency wiring: the `Container`, `NetScopeConfig`, and the
  resolved gateway -- built once, read by every screen;
- round execution: rounds run on background workers via
  `run_measurement_round`, the same use case the CLI uses -- never a
  second pipeline. Results come back as a `RoundDone` message; the
  shell keeps the latest round and delivers it to whichever screen is
  active, and again the moment a screen becomes active, so no screen
  ever shows stale data;
- incident state: one `MonitorSession` across the whole session -- an
  incident only OPENS after a 3-round sustained diagnosis and CLOSES
  after sustained health, and that state survives screen switches;
- screen routing: every screen is installed once and kept alive;
  switching screens never discards a screen's state.

Screens render and decide nothing about what the numbers mean (the
same three rules ui/cli.py documents); they degrade honestly: failed
probes render as failed, a failed round is logged, network discovery
is best-effort decoration.

TESTABILITY
-----------
`NetScopeTUI` accepts an injected `container`, `config`, and
`round_fn`, so tests drive it fully offline with fake probes and zero
wall-clock timing (set `interval` large and call `request_round()`
directly). Screens are reached with pilot key presses -- the same
offline pattern tests/test_tui.py has used since v0.2.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen

from netscope.app.config import NetScopeConfig
from netscope.app.container import Container, build_container
from netscope.app.gateway import resolve_gateway
from netscope.app.monitoring import IncidentTransition, MonitorSession
from netscope.app.use_cases import run_measurement_round
from netscope.core.models import Diagnosis, ExperienceEvent, RawMeasurement
from netscope.ui.tui.screens.dashboard import DashboardScreen

RoundFn = Callable[[Container, NetScopeConfig, str | None], tuple]


@dataclass
class RoundDone(Message):
    """One completed measurement round, posted from the worker thread
    to the UI thread (post_message is thread-safe)."""

    measurements: list[RawMeasurement] = field(default_factory=list)
    experience: ExperienceEvent | None = None
    diagnosis: Diagnosis | None = None


class NetScopeTUI(App):
    """The NetScope shell: six screens, one shared round pipeline."""

    TITLE = "NetScope"
    SUB_TITLE = "internet experience intelligence"

    BINDINGS = [
        Binding("d", "show('dashboard')", "Dashboard", priority=True),
        Binding("q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        container: Container | None = None,
        config: NetScopeConfig | None = None,
        *,
        interval: float = 15.0,
        round_fn: RoundFn | None = None,
    ):
        super().__init__()
        self._container = container
        self._config = config
        self._interval = interval
        self._round_fn = round_fn
        self._gateway: str | None = None
        self._score_history: list[float] = []
        self._round_count = 0
        self._session = MonitorSession()
        self._dashboard: DashboardScreen | None = None
        self._latest: tuple[list[RawMeasurement], ExperienceEvent, Diagnosis | None] | None = None
        self._last_transition: IncidentTransition | None = None
        self._screens: dict[str, Screen] = {}
        self._active = "dashboard"

    # ------------------------------------------------------------------
    # Wiring properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> NetScopeConfig:
        return self._config if self._config is not None else NetScopeConfig()

    @property
    def container(self) -> Container:
        if self._container is None:
            self._container = build_container()
        return self._container

    @property
    def gateway(self) -> str | None:
        return self._gateway

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def round_count(self) -> int:
        return self._round_count

    @property
    def score_history(self) -> list[float]:
        return self._score_history

    # ------------------------------------------------------------------
    # Screen routing
    # ------------------------------------------------------------------

    def get_default_screen(self) -> Screen:
        """The dashboard IS the shell's root screen -- the one the app
        boots on, the one every switch returns to, and the DOM base
        app-level queries search. Returning it here replaces Textual's
        blank `_default` screen rather than pushing another screen on
        top of it."""
        if self._dashboard is None:
            self._dashboard = DashboardScreen()
            self._screens["dashboard"] = self._dashboard
        return self._dashboard

    def on_mount(self) -> None:
        self._gateway = resolve_gateway(None, self.config)

        self.log_line(
            f"[dim]every {self._interval:g}s · gateway: {self._gateway or 'not detected'}"
            f" · keys: d dashboard · r round now · q quit[/dim]"
        )

        self.set_interval(self._interval, self.request_round)
        self.request_round()

    def _register_screen(self, name: str, screen: Screen) -> None:
        """Install one more screen (each v0.6 screen registers itself
        here exactly once, on shell mount)."""
        self._screens[name] = screen
        self.install_screen(screen, name=name)

    def action_show(self, screen_name: str) -> None:
        """Switch screens without discarding state, and re-deliver the
        latest round to the incoming screen if it consumes rounds -- a
        screen is never left showing data older than the shell's."""
        if screen_name == self._active:
            return
        target = self._screens[screen_name]
        if self.screen is not target:
            if screen_name == "dashboard":
                while self.screen is not target:
                    self.pop_screen()
            elif len(self.screen_stack) > 1:
                self.switch_screen(screen_name)
            else:
                self.push_screen(screen_name)
        self._active = screen_name

        deliver = getattr(self.screen, "on_new_round", None)
        if self._latest is not None and self._last_transition is not None and callable(deliver):
            deliver(*self._latest, self._last_transition)

    # ------------------------------------------------------------------
    # Round execution (background worker -> RoundDone message)
    # ------------------------------------------------------------------

    def request_round(self) -> None:
        """Schedule one measurement round on a background worker."""
        self._run_round_worker()

    @work(thread=True, exclusive=True, group="round")
    def _run_round_worker(self) -> None:
        container = self.container
        config = self.config
        round_fn: RoundFn = self._round_fn or run_measurement_round
        try:
            measurements, experience, diagnosis = round_fn(container, config, self._gateway)
        except Exception as exc:  # noqa: BLE001 -- a failed round is reported, never fatal
            self.call_from_thread(self.log_line, f"[red]round failed: {exc}[/red]")
            return
        self.post_message(RoundDone(measurements=measurements, experience=experience, diagnosis=diagnosis))

    def on_round_done(self, message: RoundDone) -> None:
        assert message.experience is not None
        self._round_count += 1
        self._score_history.append(message.experience.score)
        self._score_history = self._score_history[-60:]
        self._latest = (message.measurements, message.experience, message.diagnosis)
        self._last_transition = self._session.process(message.diagnosis, message.experience.timestamp)

        deliver = getattr(self.screen, "on_new_round", None)
        if callable(deliver) and self._last_transition is not None:
            deliver(*self._latest, self._last_transition)

    # ------------------------------------------------------------------
    # Shared log plumbing (the incident/event log lives on the dashboard)
    # ------------------------------------------------------------------

    def log_line(self, text: str) -> None:
        """Write one timestamped line to the session log -- the shell's
        way to report worker failures regardless of the active screen."""
        if self._dashboard is not None:
            self._dashboard._log_line(text)
