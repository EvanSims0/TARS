"""Google Calendar: read the schedule, add or move your own events, find free time.

Your own events (no other guests) are "create for you" and can be undone.
Anything that invites or notifies other people goes through the confirmation gate.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..actions import ActionResult, Tier, Tool, ToolError, schema
from ..sanitize import wrap_untrusted
from .google_auth import GoogleSession

API = "https://www.googleapis.com/calendar/v3"
CAL = f"{API}/calendars/primary"


def _when(ev: dict[str, Any], key: str) -> str:
    part = ev.get(key, {})
    return part.get("dateTime") or part.get("date") or "?"


def _describe(ev: dict[str, Any]) -> str:
    line = f"{_when(ev, 'start')} to {_when(ev, 'end')}: {ev.get('summary', '(no title)')}"
    if ev.get("location"):
        line += f" @ {ev['location']}"
    guests = [a["email"] for a in ev.get("attendees", []) if not a.get("self")]
    if guests:
        line += f" with {', '.join(guests)}"
    return line + f" [id {ev['id']}]"


def _time(value: str, tz: str) -> dict[str, str]:
    if len(value) == 10:  # all-day date
        return {"date": value}
    body = {"dateTime": value}
    if tz:
        body["timeZone"] = tz
    return body


def _zone(tz: str) -> tzinfo | None:
    try:
        return ZoneInfo(tz) if tz else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def instant(value: str, tz: str = "") -> datetime:
    """A range bound as an aware datetime: a bare date means midnight, a bare time means local time.

    Google needs RFC 3339 with an offset for time ranges and free/busy queries.
    """
    try:
        if len(value) == 10:
            parsed = datetime.combine(date.fromisoformat(value), datetime.min.time())
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"I couldn't read the time {value!r}.") from e
    if parsed.tzinfo is None:
        zone = _zone(tz)
        parsed = parsed.replace(tzinfo=zone) if zone else parsed.astimezone()
    return parsed


def _has_guests(ev: dict[str, Any]) -> bool:
    return any(not a.get("self") for a in ev.get("attendees", []))


class Calendar:
    def __init__(self, session: GoogleSession, timezone: str = ""):
        self.s = session
        self.tz = timezone

    async def events(self, start: str, end: str, query: str | None = None) -> list[dict[str, Any]]:
        params = {"timeMin": start, "timeMax": end, "singleEvents": "true", "orderBy": "startTime", "maxResults": 50}
        if query:
            params["q"] = query
        body = await self.s.request("GET", f"{CAL}/events", params=params)
        return body.get("items", [])

    async def get(self, event_id: str) -> dict[str, Any]:
        return await self.s.request("GET", f"{CAL}/events/{quote(event_id)}")

    async def insert(self, event: dict[str, Any], notify: bool = False) -> dict[str, Any]:
        params = {"sendUpdates": "all" if notify else "none"}
        return await self.s.request("POST", f"{CAL}/events", params=params, json=event)

    async def patch(self, event_id: str, changes: dict[str, Any], notify: bool = False) -> dict[str, Any]:
        params = {"sendUpdates": "all" if notify else "none"}
        return await self.s.request("PATCH", f"{CAL}/events/{quote(event_id)}", params=params, json=changes)

    async def delete(self, event_id: str) -> None:
        await self.s.request("DELETE", f"{CAL}/events/{quote(event_id)}", params={"sendUpdates": "none"})

    async def busy(self, start: str, end: str) -> list[dict[str, str]]:
        body = await self.s.request("POST", f"{API}/freeBusy", json={
            "timeMin": start, "timeMax": end, "items": [{"id": "primary"}],
        })
        return body["calendars"]["primary"].get("busy", [])


def free_slots(busy: list[dict[str, str]], start: datetime, end: datetime, minutes: int) -> list[tuple[datetime, datetime]]:
    slots, cursor = [], start
    for block in sorted(busy, key=lambda b: b["start"]):
        b_start = datetime.fromisoformat(block["start"].replace("Z", "+00:00"))
        b_end = datetime.fromisoformat(block["end"].replace("Z", "+00:00"))
        if b_start - cursor >= timedelta(minutes=minutes):
            slots.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if end - cursor >= timedelta(minutes=minutes):
        slots.append((cursor, end))
    return slots


def meeting_tally(events: list[dict[str, Any]], start: datetime, end: datetime) -> str:
    """Hours spent in meetings over a window, with recurring ones projected over a year."""
    def hours(ev: dict[str, Any]) -> float:
        s, e = ev.get("start", {}).get("dateTime"), ev.get("end", {}).get("dateTime")
        if not s or not e:
            return 0.0
        return (datetime.fromisoformat(e.replace("Z", "+00:00"))
                - datetime.fromisoformat(s.replace("Z", "+00:00"))).total_seconds() / 3600

    timed = [ev for ev in events if hours(ev) > 0]
    meetings = [ev for ev in timed if _has_guests(ev)] or timed  # personal calendars have no guests
    if not meetings:
        return "No meetings in that window."
    days = max((end - start).total_seconds() / 86400, 1)
    total = sum(hours(ev) for ev in meetings)
    series: dict[str, list[dict[str, Any]]] = {}
    for ev in meetings:
        series.setdefault(ev.get("recurringEventId") or ev["id"], []).append(ev)
    recurring = sorted(
        ((evs[0].get("summary", "(no title)"), sum(hours(e) for e in evs), len(evs))
         for key, evs in series.items() if evs[0].get("recurringEventId")),
        key=lambda r: -r[1],
    )
    lines = [f"{len(meetings)} meetings, {total:.1f} hours over {days:.0f} days "
             f"({total / days * 7:.1f} hours a week, about {total / days * 365 / 24:.0f} full days a year)."]
    for title, h, n in recurring[:5]:
        per_year = h / days * 365
        lines.append(f"Recurring '{title}': {h:.1f} h in this window ({n}x), about {per_year:.0f} hours a year.")
    return "\n".join(lines)


def build_tools(cal: Calendar) -> list[Tool]:
    def window(args: dict[str, Any]) -> tuple[datetime, datetime]:
        return instant(args["start"], cal.tz), instant(args["end"], cal.tz)

    async def list_events(args: dict[str, Any]) -> str:
        start, end = window(args)
        items = await cal.events(start.isoformat(), end.isoformat(), args.get("query"))
        if not items:
            return "Nothing on the calendar then."
        # Titles and places in other people's invites are outside text, like email.
        return wrap_untrusted("calendar", "\n".join(_describe(e) for e in items))

    async def find_free_time(args: dict[str, Any]) -> str:
        start, end = window(args)
        slots = free_slots(await cal.busy(start.isoformat(), end.isoformat()), start, end, args["minutes"])
        if not slots:
            return "No free gap that long in that window."
        return "\n".join(f"{a.isoformat()} to {b.isoformat()}" for a, b in slots[:8])

    async def add_event(args: dict[str, Any]) -> ActionResult:
        event = {"summary": args["title"], "start": _time(args["start"], cal.tz), "end": _time(args["end"], cal.tz)}
        if args.get("location"):
            event["location"] = args["location"]
        created = await cal.insert(event)

        async def undo() -> str:
            await cal.delete(created["id"])
            return f"Removed {args['title']} from your calendar."

        return ActionResult(f"Added: {_describe(created)}", undo=undo, undo_label=f"add event '{args['title']}'")

    async def move_event(args: dict[str, Any]) -> ActionResult:
        ev = await cal.get(args["event_id"])
        if _has_guests(ev):
            raise ToolError("That event has other guests, so moving it would notify them; use update_shared_event.")
        old = {"start": ev["start"], "end": ev["end"]}
        updated = await cal.patch(ev["id"], {"start": _time(args["start"], cal.tz), "end": _time(args["end"], cal.tz)})

        async def undo() -> str:
            await cal.patch(ev["id"], old)
            return "Moved it back."

        return ActionResult(f"Moved: {_describe(updated)}", undo=undo, undo_label=f"move '{ev.get('summary')}'")

    async def meeting_time(args: dict[str, Any]) -> str:
        start, end = window(args)
        return meeting_tally(await cal.events(start.isoformat(), end.isoformat()), start, end)

    async def send_invite(args: dict[str, Any]) -> str:
        event = {
            "summary": args["title"],
            "start": _time(args["start"], cal.tz),
            "end": _time(args["end"], cal.tz),
            "attendees": [{"email": e} for e in args["guests"]],
        }
        if args.get("location"):
            event["location"] = args["location"]
        created = await cal.insert(event, notify=True)
        return f"Invite sent: {_describe(created)}"

    def invite_read_back(args: dict[str, Any]) -> str:
        where = f" at {args['location']}" if args.get("location") else ""
        return (
            f"Calendar invite '{args['title']}', {args['start']} to {args['end']}{where}, "
            f"sent to {', '.join(args['guests'])}."
        )

    async def update_shared_event(args: dict[str, Any]) -> str:
        updated = await cal.patch(
            args["event_id"], {"start": _time(args["start"], cal.tz), "end": _time(args["end"], cal.tz)}, notify=True
        )
        return f"Updated and guests notified: {_describe(updated)}"

    async def update_read_back(args: dict[str, Any]) -> str:
        # Built from the event itself, not from what the model calls it.
        ev = await cal.get(args["event_id"])
        guests = [a["email"] for a in ev.get("attendees", []) if not a.get("self")]
        who = ", ".join(guests) or "its guests"
        return (
            f"Move '{ev.get('summary', '(no title)')}' to {args['start']} until {args['end']} "
            f"and notify {who}."
        )

    times = {"start": {"type": "string", "description": "ISO 8601 with offset, or YYYY-MM-DD"},
             "end": {"type": "string", "description": "ISO 8601 with offset, or YYYY-MM-DD"}}
    return [
        Tool("list_events", "Read calendar events between two times. Optional free-text `query` to find an event.",
             schema({**times, "query": {"type": "string"}}, ["start", "end"]),
             Tier.READ, list_events, cue="Checking your calendar.", service="Google Calendar"),
        Tool("find_free_time", "Find free gaps of at least `minutes` between two ISO times.",
             schema({**times, "minutes": {"type": "integer"}}, ["start", "end", "minutes"]),
             Tier.READ, find_free_time, cue="Checking your calendar.", service="Google Calendar"),
        Tool("meeting_time", "Tally hours spent in meetings between two times, including what each recurring "
             "meeting costs per year (the 'time dilation' report). Use the past 7 or 30 days unless asked.",
             schema({**times}, ["start", "end"]), Tier.READ, meeting_time, cue="Checking your calendar.",
             service="Google Calendar"),
        Tool("add_event", "Add an event or hold to the user's own calendar, with no other guests.",
             schema({"title": {"type": "string"}, **times, "location": {"type": "string"}}, ["title", "start", "end"]),
             Tier.CREATE_FOR_YOU, add_event, service="Google Calendar",
             read_back=lambda a: f"Add '{a['title']}' to your calendar, {a['start']} to {a['end']}."),
        Tool("move_event", "Move one of the user's own events (no other guests) to a new time.",
             schema({"event_id": {"type": "string"}, **times}, ["event_id", "start", "end"]),
             Tier.CREATE_FOR_YOU, move_event, service="Google Calendar",
             read_back=lambda a: f"Move that event to {a['start']} until {a['end']}."),
        Tool("send_invite", "Create an event and invite other people by email. They get notified.",
             schema({"title": {"type": "string"}, **times, "location": {"type": "string"},
                     "guests": {"type": "array", "items": {"type": "string"}}}, ["title", "start", "end", "guests"]),
             Tier.AFFECTS_OTHERS, send_invite, read_back=invite_read_back, service="Google Calendar"),
        Tool("update_shared_event", "Move an event that has other guests; they get notified.",
             schema({"event_id": {"type": "string"}, **times}, ["event_id", "start", "end"]),
             Tier.AFFECTS_OTHERS, update_shared_event, read_back=update_read_back, service="Google Calendar"),
    ]
