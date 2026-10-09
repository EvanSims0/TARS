"""The morning brief: spoken at the set time, or as soon as the PC is on after it.

It is given once a day, never during quiet hours, and waits for a conversation
in progress to finish.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from pathlib import Path

from .config import AlertConfig, BriefConfig

BRIEF_REQUEST = "[Scheduled] Give me my morning brief."


def _parse(hhmm: str) -> time:
    hour, minute = hhmm.split(":")
    return time(int(hour), int(minute))


def in_quiet_hours(now: time, alerts: AlertConfig) -> bool:
    start, end = _parse(alerts.quiet_start), _parse(alerts.quiet_end)
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight


class BriefScheduler:
    def __init__(
        self,
        config: BriefConfig,
        alerts: AlertConfig,
        state_path: Path,
        deliver: Callable[[], Awaitable[None]],
        busy: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.config = config
        self.alerts = alerts
        self.state_path = state_path
        self.deliver = deliver
        self.busy = busy
        self.clock = clock

    def _last(self) -> date | None:
        try:
            return date.fromisoformat(self.state_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return None

    def _mark(self, day: date) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(day.isoformat(), encoding="utf-8")

    def due(self) -> bool:
        now = self.clock()
        return (
            self.config.enabled
            and now.time() >= _parse(self.config.time)
            and self._last() != now.date()
            and not in_quiet_hours(now.time(), self.alerts)
        )

    async def tick(self) -> bool:
        if not self.due() or self.busy():
            return False
        self._mark(self.clock().date())  # mark first so a failure can't repeat it all day
        await self.deliver()
        return True

    async def run(self, every_seconds: float = 30) -> None:
        # Checked on a short interval so it also fires soon after the PC wakes from sleep.
        while True:
            await self.tick()
            await asyncio.sleep(every_seconds)
