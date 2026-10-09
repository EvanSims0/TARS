"""Tools the model can call, each tagged with an action tier.

The tier decides, in code, what happens when the model asks for the action:

- READ: runs immediately.
- CREATE_FOR_YOU: runs immediately, is summarised in one line, and can be undone.
- AFFECTS_OTHERS: never runs from a model call. The confirmation gate reads it
  back in full and waits for an explicit yes from the user.
- CONFIRM: held for an explicit yes on any channel (e.g. raising TARS's trust level).
- IRREVERSIBLE: not supported in v1; such tools cannot even be registered.

When the user lowers TARS's trust, CREATE_FOR_YOU actions on their accounts are
held for a yes too.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class Tier(enum.Enum):
    READ = "read"
    CREATE_FOR_YOU = "create_for_you"
    AFFECTS_OTHERS = "affects_others"
    CONFIRM = "confirm"
    IRREVERSIBLE = "irreversible"


class Channel(enum.Enum):
    PC = "pc"
    PHONE = "phone"


class ToolError(Exception):
    """A failure to report to the user in one plain sentence."""


@dataclass
class ActionResult:
    """What a tool hands back to the model.

    ``content`` goes to the model as the tool result. ``untrusted`` marks content
    that came from outside (email bodies, web text) so the conversation can be
    tainted. ``undo`` reverses a CREATE_FOR_YOU action.
    """

    content: str
    untrusted: bool = False
    undo: Callable[[], Awaitable[str]] | None = None
    undo_label: str = ""


Handler = Callable[[dict[str, Any]], Awaitable[ActionResult | str]]
ReadBack = Callable[[dict[str, Any]], str | Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    tier: Tier
    handler: Handler
    # A short spoken cue, said if this tool runs before anything else was spoken.
    cue: str = ""
    # AFFECTS_OTHERS tools must say exactly what will happen, built from the inputs.
    read_back: ReadBack | None = None
    # Phone-channel fallback for AFFECTS_OTHERS tools: e.g. save a draft instead of sending.
    park: Handler | None = None
    # Integration name, for /status and "which service is down" messages. Empty for local tools.
    service: str = ""
    # For tools whose tier depends on the inputs (e.g. lowering vs raising trust).
    tier_for: Callable[[dict[str, Any]], Tier] | None = None

    def __post_init__(self) -> None:
        if self.tier is Tier.IRREVERSIBLE:
            raise ValueError(f"{self.name}: irreversible or money actions are not supported in v1")
        if self.tier is Tier.AFFECTS_OTHERS and self.read_back is None:
            raise ValueError(f"{self.name}: actions that affect others need a read-back")

    def effective_tier(self, args: dict[str, Any]) -> Tier:
        tier = self.tier_for(args) if self.tier_for else self.tier
        if tier is Tier.IRREVERSIBLE:
            raise ValueError(f"{self.name}: irreversible actions are not supported")
        return tier

    def describe(self, args: dict[str, Any]) -> str:
        """A one-line spoken summary of the action, for confirmations without a custom read-back."""
        values = ", ".join(str(v) if not isinstance(v, list) else " and ".join(map(str, v))
                           for v in args.values() if v not in (None, "", []))
        return f"{self.name.replace('_', ' ').capitalize()}: {values}." if values else f"{self.name.replace('_', ' ').capitalize()}."

    def api_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, *tools: Tool) -> None:
        for tool in tools:
            if tool.name in self.tools:
                raise ValueError(f"Duplicate tool: {tool.name}")
            self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def api_definitions(self) -> list[dict[str, Any]]:
        # Sorted so the tool list is byte-identical between requests and stays cached.
        return [self.tools[name].api_definition() for name in sorted(self.tools)]


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
