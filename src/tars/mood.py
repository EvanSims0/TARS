"""Reads the room: discretion while presenting, and less humor when you're stressed.

Presenting: a Zoom or Teams meeting is running on the PC, or the current calendar
event looks like a presentation. Stress: back-to-back meetings coming up, or tasks
due today still open late at night. Checked every few minutes in the background.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from .personality import Mood, Personality

PRESENTING_WORDS = ("present", "demo", "interview", "pitch", "webinar", "keynote", "#presenting")
# Processes that only run during a call or while sharing a screen.
MEETING_PROCESSES = {
    "cpthost.exe": "Zoom meeting",       # Zoom's meeting/screen-share host
    "aomhost64.exe": "Zoom meeting",
    "webexhost.exe": "Webex meeting",
}
BACK_TO_BACK_GAP = timedelta(minutes=10)
BACK_TO_BACK_COUNT = 3
LATE_NIGHT_HOUR = 21

Events = Callable[[datetime, datetime], Awaitable[list[dict[str, Any]]]]
OpenTasksDueToday = Callable[[], Awaitable[int]]


def _parse(value: dict[str, str]) -> datetime | None:
    raw = value.get("dateTime")
    if not raw:
        return None  # all-day events don't make you busy
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def presenting_event(events: list[dict[str, Any]], now: datetime) -> str:
    for ev in events:
        start, end = _parse(ev.get("start", {})), _parse(ev.get("end", {}))
        if not start or not end or not (start <= now < end):
            continue
        text = f"{ev.get('summary', '')} {ev.get('description', '')}".lower()
        if any(word in text for word in PRESENTING_WORDS):
            return f"calendar: {ev.get('summary', 'presentation')}"
    return ""


def back_to_back(events: list[dict[str, Any]], now: datetime, horizon: timedelta = timedelta(hours=3)) -> bool:
    """At least three timed events in the next few hours with no real break between them."""
    spans = sorted(
        (s, e) for ev in events
        if (s := _parse(ev.get("start", {}))) and (e := _parse(ev.get("end", {}))) and e > now and s < now + horizon
    )
    run, last_end = 0, None
    for start, end in spans:
        run = run + 1 if last_end is not None and start - last_end <= BACK_TO_BACK_GAP else 1
        last_end = max(end, last_end) if last_end else end
        if run >= BACK_TO_BACK_COUNT:
            return True
    return False


def meeting_process() -> str:
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return ""
    for proc, label in MEETING_PROCESSES.items():
        if f'"{proc}"' in out:
            return label
    if '"ms-teams.exe"' in out and _teams_in_call():
        return "Teams meeting"
    return ""


def _teams_in_call() -> bool:
    """New Teams shows a separate meeting window titled like 'Meeting | Microsoft Teams'."""
    try:
        import ctypes
        from ctypes import wintypes

        titles: list[str] = []
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value.lower())
            return True

        user32.EnumWindows(collect, 0)
        return any("microsoft teams" in t and ("meeting" in t or "call" in t) for t in titles)
    except Exception:
        return False


class MoodMonitor:
    def __init__(
        self,
        personality: Personality,
        events: Events | None = None,
        open_tasks_due_today: OpenTasksDueToday | None = None,
        detect_meeting: Callable[[], str] = meeting_process,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        periodic: list[Callable[[], Awaitable[Any]]] | None = None,
    ):
        self.personality = personality
        self.events = events
        self.open_tasks_due_today = open_tasks_due_today
        self.detect_meeting = detect_meeting
        self.clock = clock
        self.periodic = periodic or []

    async def refresh(self) -> Mood:
        now = self.clock()
        mood = Mood()
        events: list[dict[str, Any]] = []
        if self.events is not None:
            try:
                events = await self.events(now - timedelta(hours=1), now + timedelta(hours=3))
            except Exception as e:  # a calendar hiccup shouldn't stop the assistant
                logger.debug(f"mood: calendar unavailable: {e}")

        if reason := await asyncio.to_thread(self.detect_meeting) or presenting_event(events, now):
            mood.presenting, mood.presenting_reason = True, reason
        if back_to_back(events, now):
            mood.stressed, mood.stress_reason = True, "back-to-back meetings"
        elif now.hour >= LATE_NIGHT_HOUR and self.open_tasks_due_today is not None:
            try:
                if await self.open_tasks_due_today() > 0:
                    mood.stressed, mood.stress_reason = True, "late-night deadline"
            except Exception as e:
                logger.debug(f"mood: tasks unavailable: {e}")
        self.personality.mood = mood
        return mood

    async def run(self, every_seconds: float = 300) -> None:
        while True:
            await self.refresh()
            for job in self.periodic:
                try:
                    await job()
                except Exception as e:
                    logger.debug(f"periodic job failed: {e}")
            await asyncio.sleep(every_seconds)
