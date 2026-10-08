"""Google Calendar: read the schedule, add or move your own events, find free time.

Your own events (no other guests) are "create for you" and can be undone.
Anything that invites or notifies other people goes through the confirmation gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from ..actions import ActionResult, Tier, Tool, ToolError, schema
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


def build_tools(cal: Calendar) -> list[Tool]:
    async def list_events(args: dict[str, Any]) -> str:
        items = await cal.events(args["start"], args["end"], args.get("query"))
        if not items:
            return "Nothing on the calendar then."
        return "\n".join(_describe(e) for e in items)

    async def find_free_time(args: dict[str, Any]) -> str:
        start = datetime.fromisoformat(args["start"])
        end = datetime.fromisoformat(args["end"])
        slots = free_slots(await cal.busy(args["start"], args["end"]), start, end, args["minutes"])
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

    def update_read_back(args: dict[str, Any]) -> str:
        return (
            f"Move event {args['event_id']} to {args['start']} until {args['end']} "
            f"and notify all its guests ({args.get('summary', 'event')})."
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
        Tool("add_event", "Add an event or hold to the user's own calendar, with no other guests.",
             schema({"title": {"type": "string"}, **times, "location": {"type": "string"}}, ["title", "start", "end"]),
             Tier.CREATE_FOR_YOU, add_event, service="Google Calendar"),
        Tool("move_event", "Move one of the user's own events (no other guests) to a new time.",
             schema({"event_id": {"type": "string"}, **times}, ["event_id", "start", "end"]),
             Tier.CREATE_FOR_YOU, move_event, service="Google Calendar"),
        Tool("send_invite", "Create an event and invite other people by email. They get notified.",
             schema({"title": {"type": "string"}, **times, "location": {"type": "string"},
                     "guests": {"type": "array", "items": {"type": "string"}}}, ["title", "start", "end", "guests"]),
             Tier.AFFECTS_OTHERS, send_invite, read_back=invite_read_back, service="Google Calendar"),
        Tool("update_shared_event", "Move an event that has other guests; they get notified.",
             schema({"event_id": {"type": "string"}, **times, "summary": {"type": "string"}},
                    ["event_id", "start", "end", "summary"]),
             Tier.AFFECTS_OTHERS, update_shared_event, read_back=update_read_back, service="Google Calendar"),
    ]
