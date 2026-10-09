"""What TARS is doing right now, for the overlay and the tray icon.

The voice pipeline and the agent write to it from the event loop; the app server reads it from
its own thread, and long-polls on `version` so the overlay updates within a frame of a change.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

STATES = ("idle", "listening", "thinking", "speaking", "confirm", "muted", "problem")
# What a failing service means for the user, and the one next step to offer.
NEXT_STEPS = {
    "Google Calendar": ("GCAL", "reconnect Google in Settings → Accounts. Email, tasks and jokes still work."),
    "Gmail": ("GMAIL", "reconnect Google in Settings → Accounts. Calendar and tasks still work."),
    "Todoist": ("TODOIST", "check the Todoist token in Settings → Accounts. Calendar and email still work."),
    "Google Maps": ("MAPS", "travel times need the optional Google Maps key in Settings → Accounts."),
    "Open-Meteo": ("WEATHER", "the weather service is down; try again in a few minutes."),
    "Claude": ("CLAUDE", "check the internet connection, or the Claude key in Settings → Accounts."),
}
JOKE_MARK = "⁂"  # ⁂ starts a sentence the model meant as a joke; never spoken, lights the cue light


@dataclass
class Live:
    state: str = "idle"
    user_text: str = ""
    user_final: bool = False
    reply_text: str = ""
    cue: str = ""                     # "Checking your calendar." while a tool runs
    joke: bool = False                # the current reply has a line marked as a joke
    card: dict[str, Any] | None = None
    confirm: dict[str, Any] | None = None
    problem: dict[str, str] | None = None
    turn_started: float = 0.0
    first_word_ms: int | None = None
    mic_closes_at: float = 0.0        # when the follow-up window closes, for the countdown
    follow_up_seconds: float = 8.0
    brief: bool = False
    version: int = 0
    updated: float = field(default_factory=time.time)


class LiveState:
    def __init__(self, follow_up_seconds: float = 8.0):
        self._live = Live(follow_up_seconds=follow_up_seconds)
        self._cond = threading.Condition()

    def _change(self, **values: Any) -> None:
        with self._cond:
            for key, value in values.items():
                setattr(self._live, key, value)
            self._live.version += 1
            self._live.updated = time.time()
            self._cond.notify_all()

    # Writers (event loop)

    def set_state(self, state: str) -> None:
        state = {"needs_confirmation": "confirm"}.get(state, state)
        if state not in STATES:
            return
        if state == "idle" and self._live.problem:
            state = "problem"  # a service is still down; the tray keeps saying so until a turn succeeds
        if state == self._live.state:
            return
        self._change(state=state)

    def user_partial(self, text: str) -> None:
        self._change(user_text=text, user_final=False)

    def turn_started(self, text: str, brief: bool = False) -> None:
        self._change(state="thinking", user_text=text, user_final=True, reply_text="", cue="", joke=False,
                     card=None, confirm=None, problem=None, turn_started=time.time(), first_word_ms=None, brief=brief)

    def reply(self, text: str) -> None:
        live = self._live
        first = live.first_word_ms
        if first is None and text.strip() and live.turn_started:
            first = round((time.time() - live.turn_started) * 1000)
        self._change(reply_text=live.reply_text + text, first_word_ms=first)

    def cue_said(self, cue: str) -> None:
        self._change(cue=cue.strip())

    def joked(self) -> None:
        self._change(joke=True)

    def show_card(self, card: dict[str, Any]) -> None:
        self._change(card=card)

    def held(self, confirm: dict[str, Any] | None) -> None:
        self._change(confirm=confirm, state="confirm" if confirm else self._live.state)

    def failed(self, service: str, message: str = "") -> None:
        short, step = NEXT_STEPS.get(service, (service.upper()[:8], "try again in a moment."))
        self._change(state="problem", problem={"service": service, "short": short, "message": message, "next": step})

    def mic_window(self, closes_at: float) -> None:
        self._change(mic_closes_at=closes_at)

    def clear_problem(self) -> None:
        self._change(problem=None, state="idle" if self._live.state == "problem" else self._live.state)

    # Readers (server thread)

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return asdict(self._live)

    def wait(self, since: int, timeout: float = 15.0) -> dict[str, Any]:
        """Block until something changes after `since` (or the timeout), then return the state."""
        with self._cond:
            self._cond.wait_for(lambda: self._live.version > since, timeout=timeout)
            return asdict(self._live)
