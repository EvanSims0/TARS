"""Tools that run entirely on the PC: timers, memory, undo."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .actions import ActionResult, Tier, Tool, ToolError, schema
from .memory import HEADINGS, Vault
from .personality import CALM_NAME, Personality, PersonalitySettings
from .transcripts import Transcripts

Announce = Callable[[str], Awaitable[None]]


@dataclass
class UndoEntry:
    label: str
    undo: Callable[[], Awaitable[str]]
    at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


class UndoStack:
    def __init__(self, limit: int = 20):
        self.entries: list[UndoEntry] = []
        self.limit = limit
        self.on_undone: Callable[[str], None] | None = None

    def push(self, label: str, undo: Callable[[], Awaitable[str]]) -> str:
        entry = UndoEntry(label, undo)
        self.entries.append(entry)
        del self.entries[: -self.limit]
        return entry.id

    def has(self, entry_id: str) -> bool:
        return any(e.id == entry_id for e in self.entries)

    async def pop(self, entry_id: str | None = None) -> str:
        """Undo the latest action, or a specific one (the History window's Undo buttons)."""
        if not self.entries:
            raise ToolError("There's nothing to undo.")
        if entry_id is None:
            entry = self.entries.pop()
        else:
            entry = next((e for e in self.entries if e.id == entry_id), None)
            if entry is None:
                raise ToolError("That can't be undone any more.")
            self.entries.remove(entry)
        message = await entry.undo()
        if self.on_undone is not None:
            self.on_undone(entry.id)
        return message


class Timers:
    def __init__(self, announce: Announce):
        self.announce = announce
        self._timers: dict[str, tuple[float, asyncio.Task]] = {}

    def start(self, name: str, seconds: float) -> None:
        self.cancel(name)

        async def ring() -> None:
            await asyncio.sleep(seconds)
            self._timers.pop(name, None)
            await self.announce(f"Your {name} timer is done.")

        self._timers[name] = (time.time() + seconds, asyncio.create_task(ring()))

    def cancel(self, name: str) -> bool:
        entry = self._timers.pop(name, None)
        if entry:
            entry[1].cancel()
        return entry is not None

    def remaining(self) -> dict[str, float]:
        now = time.time()
        return {name: max(0.0, end - now) for name, (end, _) in self._timers.items()}


def _fmt(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    h, m = divmod(m, 60)
    parts = [f"{h} hour{'s' * (h != 1)}" if h else "", f"{m} minute{'s' * (m != 1)}" if m else "",
             f"{s} second{'s' * (s != 1)}" if s and not h else ""]
    return " ".join(p for p in parts if p) or "0 seconds"


PersonalityListener = Callable[[Personality], Awaitable[None]]


def personality_tool(personality: Personality, listeners: list[PersonalityListener] | None = None) -> Tool:
    listeners = listeners if listeners is not None else []

    def tier_for(args: dict[str, Any]) -> Tier:
        # Raising trust means fewer confirmations, so it needs the user's own yes.
        if args.get("trust") is not None and args["trust"] > personality.settings.trust:
            return Tier.CONFIRM
        return Tier.CREATE_FOR_YOU

    def read_back(args: dict[str, Any]) -> str:
        return f"Raise trust to {args['trust']}%, so I stop asking before changing your lists, calendar and email filing."

    async def notify() -> None:
        for listener in listeners:
            await listener(personality)

    async def set_personality(args: dict[str, Any]) -> ActionResult:
        before = PersonalitySettings(**vars(personality.settings))
        mode = args.get("mode")
        summary = personality.update(
            humor=args.get("humor"), bluntness=args.get("bluntness"), trust=args.get("trust"),
            calm=None if mode is None else mode == "calm",
        )
        await notify()

        async def restore() -> str:
            personality.restore(before)
            await notify()
            return "Settings put back."

        return ActionResult(summary, undo=restore, undo_label="personality change")

    return Tool(
        "set_personality",
        f"Change TARS's humor, bluntness or trust (0-100), or switch mode: 'calm' for {CALM_NAME}, the "
        "joke-free assistant, or 'tars' for TARS. Lower trust means TARS asks before changing the user's "
        "accounts.",
        schema({"humor": {"type": "integer"}, "bluntness": {"type": "integer"}, "trust": {"type": "integer"},
                "mode": {"type": "string", "enum": ["tars", "calm"]}}),
        Tier.CREATE_FOR_YOU, set_personality, read_back=read_back, tier_for=tier_for,
    )


def memory_map_tool(open_map: Callable[[], str]) -> Tool:
    async def show(args: dict[str, Any]) -> str:
        open_map()
        return "The memory map is open in the browser on the PC."

    return Tool("show_memory_map", "Open the memory map: everything TARS remembers, on the PC screen, to browse, "
                "search and forget facts. Use when the user asks to see or browse their memory.",
                schema({}), Tier.READ, show)


def build_tools(vault: Vault, transcripts: Transcripts, timers: Timers, undo: UndoStack) -> list[Tool]:
    async def set_timer(args: dict[str, Any]) -> ActionResult:
        name = (args.get("name") or "kitchen").strip().lower()
        seconds = args["seconds"]
        if seconds <= 0 or seconds > 24 * 3600:
            raise ToolError("Timers can run from a second up to a day.")
        timers.start(name, seconds)

        async def cancel() -> str:
            timers.cancel(name)
            return f"Cancelled the {name} timer."

        return ActionResult(f"{name} timer set for {_fmt(seconds)}.", undo=cancel, undo_label=f"{name} timer")

    async def list_timers(args: dict[str, Any]) -> str:
        running = timers.remaining()
        if not running:
            return "No timers running."
        return "\n".join(f"{n}: {_fmt(s)} left" for n, s in running.items())

    async def cancel_timer(args: dict[str, Any]) -> str:
        name = args["name"].strip().lower()
        return f"Cancelled the {name} timer." if timers.cancel(name) else f"There's no {name} timer."

    async def remember(args: dict[str, Any]) -> ActionResult:
        reply = vault.remember(args["fact"], args.get("category") or "Other")

        async def drop() -> str:
            vault.forget(args["fact"])
            return "Forgot it."

        return ActionResult(reply, undo=drop, undo_label="remember")

    async def forget(args: dict[str, Any]) -> str:
        removed = vault.forget(args.get("about"))
        dropped = 0 if args.get("about") else transcripts.forget_last_exchange()
        if not removed and not dropped:
            return "There was nothing matching to forget."
        what = f"Forgot: {'; '.join(removed)}." if removed else "Forgot that."
        return what

    async def do_undo(args: dict[str, Any]) -> str:
        return await undo.pop()

    return [
        Tool("set_timer", "Start a named countdown timer. Several can run at once.",
             schema({"seconds": {"type": "integer"}, "name": {"type": "string"}}, ["seconds"]),
             Tier.CREATE_FOR_YOU, set_timer),
        Tool("list_timers", "Say which timers are running and how long is left.",
             schema({}), Tier.READ, list_timers),
        Tool("cancel_timer", "Cancel a named timer.",
             schema({"name": {"type": "string"}}, ["name"]), Tier.CREATE_FOR_YOU, cancel_timer),
        Tool("remember", "Save a lasting fact about the user's life (people, preferences, places, health, "
             "routines) to memory. Use whenever the user shares one, without being asked.",
             schema({"fact": {"type": "string"}, "category": {"type": "string", "enum": HEADINGS}}, ["fact"]),
             Tier.CREATE_FOR_YOU, remember),
        Tool("forget", "Forget a saved fact. With `about`, removes matching facts; without it, removes the "
             "last thing saved and the last exchange from the transcript ('forget that').",
             schema({"about": {"type": "string"}}), Tier.CREATE_FOR_YOU, forget),
        Tool("undo", "Undo the most recent action TARS took (task added, event added, email archived, ...).",
             schema({}), Tier.CREATE_FOR_YOU, do_undo),
    ]
