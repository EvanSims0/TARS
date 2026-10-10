"""The morning brief: spoken at the set time, or as soon as the PC is on after it.

It is given once a day, never during quiet hours, and waits for a conversation
in progress to finish.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def local_day(now: datetime, timezone: str = "") -> tuple[datetime, datetime]:
    """Today from midnight to midnight in the user's timezone.

    On the day the clocks change that is 23 or 25 hours; taking today's UTC offset back to
    midnight would start the day an hour early or late.
    """
    try:
        zone = ZoneInfo(timezone) if timezone else (now.tzinfo or now.astimezone().tzinfo)
    except (ZoneInfoNotFoundError, ValueError):
        zone = now.astimezone().tzinfo
    day = now.astimezone(zone).date()
    return datetime.combine(day, time.min, zone), datetime.combine(day + timedelta(days=1), time.min, zone)


async def mission_card(app: Any, now: datetime | None = None) -> dict[str, Any]:
    """The overlay's pre-launch checklist: one GO/HOLD row per system, from real data.

    Every row is optional; a service that isn't connected or doesn't answer is left out.
    """
    now = now or datetime.now().astimezone()
    day_start, day_end = local_day(now, app.config.location.timezone)
    now = now.astimezone(day_start.tzinfo)
    rows: list[dict[str, str]] = []

    async def tool(name: str, args: dict[str, Any]) -> str | None:
        found = app.agent.registry.get(name)
        if found is None:
            return None
        try:
            out = await found.handler(args)
        except Exception:
            return None
        return getattr(out, "content", out)

    weather = await tool("get_weather", {})
    if weather:
        first = weather.splitlines()[0].split("now: ", 1)[-1].split(" (feels")[0]
        bad = any(w in first for w in ("rain", "snow", "thunder", "showers", "freezing"))
        rows.append({"label": "WEATHER", "text": first[:1].upper() + first[1:], "status": "HOLD" if bad else "GO"})

    lead = ""
    if app.mood.events is not None:
        try:
            events = await app.mood.events(day_start, day_end)
        except Exception:
            events = None
        if events is not None:
            timed = [e for e in events if e.get("start", {}).get("dateTime")]
            starts = [datetime.fromisoformat(e["start"]["dateTime"].replace("Z", "+00:00")) for e in timed]
            first_at = f", first at {starts[0].hour % 12 or 12}:{starts[0]:%M}" if starts else ""
            rows.append({"label": "CALENDAR", "text": f"{len(events)} item{'s' * (len(events) != 1)}{first_at}",
                         "status": "GO"})
            upcoming = [s for s in starts if s > now]
            if upcoming:
                lead = f"T−{max(1, round((upcoming[0] - now).total_seconds() / 60))} min to first meeting"
            placed = [(s, e) for s, e in zip(starts, timed) if e.get("location") and s > now]
            if placed:
                s, e = placed[0]
                rows.append({"label": e.get("summary", "NEXT")[:10].upper(),
                             "text": f"{s.hour % 12 or 12}:{s:%M} · {e['location'].split(',')[0]}", "status": "GO"})

    inbox = await tool("list_email", {"query": "is:unread in:inbox", "limit": 25})
    if inbox is not None:
        unread = inbox.count("[id ")
        rows.append({"label": "INBOX", "text": f"{unread}{'+' if unread == 25 else ''} unread" if unread else "Clear",
                     "status": "HOLD" if unread else "GO"})

    spend, cap = app.ledger.month_total(), app.config.spend.monthly_cap_usd
    rows.append({"label": "SPEND", "text": f"${spend:.2f} of ${cap:.0f} this month",
                 "status": "HOLD" if spend >= cap * app.config.spend.escalation_cutoff else "GO"})
    return {"kind": "brief", "title": f"MISSION BRIEF · {now:%a} {now.day} {now:%b}".upper(),
            "lead": lead or "No meetings ahead today", "rows": rows}
