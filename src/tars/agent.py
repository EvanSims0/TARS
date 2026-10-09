"""TARS's own streaming tool loop on the Claude API.

Claude Haiku 5.5 answers by default with thinking off, for speed. When a request
needs multi-step planning the model calls ``think_harder`` and the rest of the
turn runs on Claude Sonnet 5.5, unless spend is near the monthly cap.

Every tool call passes through the confirmation gate before it can run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .actions import ActionResult, Channel, ToolError, ToolRegistry
from .config import BrainConfig
from .gate import ConfirmationGate
from .memory import Vault
from .persona import SYSTEM_PROMPT
from .personality import Personality
from .spend import SpendLedger, SpendState, TurnLog
from .local_tools import UndoStack
from .transcripts import Transcripts

OnText = Callable[[str], Awaitable[None]]

THINK_HARDER = {
    "name": "think_harder",
    "description": "Hand this request to a more capable planner. Use for multi-step tasks: comparing "
                   "options across days, juggling several events, or drafting something long.",
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}

CONVERSATION_IDLE_SECONDS = 10 * 60


class Backend(Protocol):
    async def stream(self, *, on_text: OnText, **request: Any) -> Any:
        """Run one streaming Messages request, calling ``on_text`` per text delta; return the final message."""


class AnthropicBackend:
    def __init__(self, api_key: str | None = None, http_client: Any = None):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, max_retries=1, timeout=30, http_client=http_client)

    async def stream(self, *, on_text: OnText, **request: Any) -> Any:
        async with self.client.beta.messages.stream(**request) as stream:
            async for event in stream:
                if event.type == "text":
                    await on_text(event.text)
            return await stream.get_final_message()


def block_to_param(block: Any) -> dict[str, Any]:
    """Turn a response content block into a request param, keeping thinking signatures intact."""
    kind = block.type
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    return block.model_dump(mode="json", exclude_none=True)


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "array": list, "object": dict}


def validate_input(value: Any, schema: dict[str, Any], path: str = "input") -> str | None:
    """A small JSON-schema check for tool inputs (eager streaming skips the server's)."""
    kind = schema.get("type")
    if kind and not isinstance(value, _JSON_TYPES[kind]) or (kind == "integer" and isinstance(value, bool)):
        return f"{path} should be {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} should be one of {schema['enum']}"
    if kind == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                return f"{path}.{req} is required"
        for key, sub in value.items():
            if key not in props:
                if schema.get("additionalProperties") is False:
                    return f"{path}.{key} is not allowed"
                continue
            if err := validate_input(sub, props[key], f"{path}.{key}"):
                return err
    if kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            if err := validate_input(item, schema["items"], f"{path}[{i}]"):
                return err
    return None


@dataclass
class TurnResult:
    text: str
    awaiting_confirmation: bool = False
    models: list[str] = field(default_factory=list)
    usd: float = 0.0
    first_text_ms: int | None = None
    tools: list[str] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        backend: Backend,
        registry: ToolRegistry,
        gate: ConfirmationGate,
        ledger: SpendLedger,
        turn_log: TurnLog,
        vault: Vault,
        transcripts: Transcripts,
        undo: UndoStack,
        config: BrainConfig,
        timezone: str = "",
        user_name: str = "",
        user_email: str = "",
        instructions: str = "",
        personality: Personality | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.backend = backend
        self.registry = registry
        self.gate = gate
        self.ledger = ledger
        self.turn_log = turn_log
        self.vault = vault
        self.transcripts = transcripts
        self.undo = undo
        self.config = config
        self.timezone = timezone
        self.user_name = user_name
        self.user_email = user_email
        self.instructions = instructions
        self.personality = personality or Personality()
        self.clock = clock
        self.messages: list[dict[str, Any]] = []
        self._producers: list[str | None] = []  # which model wrote each message
        self.tainted = False
        self._memory_snapshot = ""
        self._last_turn = 0.0
        self._turn_spoke = False

    # Conversation state

    def reset(self) -> None:
        self.messages, self._producers = [], []
        self.tainted = False
        self.gate.cancel()
        self._memory_snapshot = self.vault.snapshot()

    def _append(self, role: str, content: list[dict[str, Any]], producer: str | None = None) -> None:
        self.messages.append({"role": role, "content": content})
        self._producers.append(producer)

    def _messages_for(self, model: str) -> list[dict[str, Any]]:
        # Thinking blocks are bound to the model that wrote them; drop other models' blocks.
        out = []
        for msg, producer in zip(self.messages, self._producers):
            if msg["role"] == "assistant" and producer not in (None, model):
                content = [b for b in msg["content"] if b["type"] not in ("thinking", "redacted_thinking")]
                msg = {"role": "assistant", "content": content or [{"type": "text", "text": "(done)"}]}
            out.append(msg)
        return out

    def _now(self) -> datetime:
        """The current time in the user's timezone, with its UTC offset."""
        now = self.clock()
        try:
            tz = ZoneInfo(self.timezone) if self.timezone else None
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
        if now.tzinfo is None:  # a naive clock reads the user's local time
            return now.replace(tzinfo=tz) if tz else now.astimezone()
        return now.astimezone(tz) if tz else now

    def _context_line(self, channel: Channel) -> str:
        now = self._now()
        offset = now.strftime("%z")
        # Calendar times need an offset, and guessing one across a DST change goes wrong.
        tz = f" ({self.timezone}, UTC{offset[:3]}:{offset[3:]})" if self.timezone else f" (UTC{offset[:3]}:{offset[3:]})"
        who = f" The user is {self.user_name}." if self.user_name else ""
        if self.user_email:
            who += f" Their email address is {self.user_email}."
        where = "spoken at the PC" if channel is Channel.PC else "sent from the phone via Telegram"
        return f"[{now:%A %d %B %Y, %H:%M}{tz}. Message {where}.{who} {self.personality.context()}]"

    def _request(self, model: str, deep: bool) -> dict[str, Any]:
        tools = [{**t, "eager_input_streaming": True} for t in self.registry.api_definitions()]
        if not deep:
            tools.append({**THINK_HARDER, "eager_input_streaming": True})
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        if self.instructions:
            system.append({"type": "text", "text": self.instructions})
        if self._memory_snapshot:
            system.append({"type": "text", "text": "What you remember about the user:\n" + self._memory_snapshot})
        request: dict[str, Any] = {
            "model": model,
            "system": system,
            "tools": tools,
            "messages": self._messages_for(model),
            "cache_control": {"type": "ephemeral"},
        }
        if deep:
            request.update(
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={"effort": self.config.deep_effort},
                # Server-side fallback if a safety classifier declines the request.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        else:
            request.update(
                max_tokens=self.config.max_tokens,
                thinking={"type": "adaptive"} if self.config.fast_thinking else {"type": "disabled"},
                output_config={"effort": self.config.fast_effort},
            )
        return request

    # One user turn

    async def handle(self, text: str, on_text: OnText, channel: Channel = Channel.PC) -> TurnResult:
        start = time.monotonic()
        result = TurnResult(text="")
        spoken: list[str] = []

        self._turn_spoke = False

        async def say(chunk: str) -> None:
            if result.first_text_ms is None and chunk.strip():
                result.first_text_ms = round((time.monotonic() - start) * 1000)
            self._turn_spoke = self._turn_spoke or bool(chunk.strip())
            spoken.append(chunk)
            await on_text(chunk)

        if time.time() - self._last_turn > CONVERSATION_IDLE_SECONDS and not self.gate.has_pending():
            self.reset()
        self._last_turn = time.time()
        self.transcripts.append("user", text, channel.value)

        try:
            await self._turn(text, channel, say, result)
        except asyncio.CancelledError:
            # Talked over or stopped: leave the history valid for the next request.
            self._repair_history()
            raise
        finally:
            result.text = "".join(spoken).strip()
            self.transcripts.append("assistant", result.text, channel.value)
            self.turn_log.record(
                channel=channel.value,
                first_word_ms=result.first_text_ms,
                total_ms=round((time.monotonic() - start) * 1000),
                models=result.models,
                usd=round(result.usd, 6),
                tools=result.tools,
                held=result.awaiting_confirmation,
            )
        return result

    async def _turn(self, text: str, channel: Channel, say: OnText, result: TurnResult) -> None:
        note = ""
        if self.gate.has_pending():
            res = await self.gate.resolve(text, channel)
            if not res.passthrough:
                if res.result is not None:
                    self._remember_undo(res.result.undo_label, res.result)
                self._append("user", [{"type": "text", "text": text}])
                self._append("assistant", [{"type": "text", "text": res.message}])
                await say(res.message)
                return
            note = "\n[System: the held action was not carried out because the reply wasn't a clear yes.]"

        state = self.ledger.state()
        if state is SpendState.CAPPED:
            await say(
                f"I've reached this month's ${self.ledger.config.monthly_cap_usd:.0f} spending cap, "
                "so I'm paused until next month. You can raise the cap in settings."
            )
            return

        if notice := self.personality.stress_notice():
            await say(notice)
        self._append("user", [{"type": "text", "text": f"{self._context_line(channel)}\n{text}{note}"}])
        deep = False
        json_retries = 0
        for _ in range(self.config.max_tool_rounds):
            model = self.config.deep_model if deep else self.config.fast_model
            try:
                response = await self.backend.stream(on_text=say, **self._request(model, deep))
            except ValueError:
                # Tool input JSON the SDK couldn't parse at all; re-issue the request.
                json_retries += 1
                if json_retries > 2:
                    raise
                continue
            json_retries = 0
            result.models.append(model)
            result.usd += self.ledger.record_llm(model, response.usage)
            content = [block_to_param(b) for b in response.content] or [{"type": "text", "text": "Okay."}]
            self._append("assistant", content, model)

            if response.stop_reason == "refusal":
                await say("I can't help with that one.")
                return
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return
            if response.stop_reason == "max_tokens":
                # A truncated tool input parses as a partial object; never run it.
                self._append("user", [{"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                       "content": "Input was cut off; not run."} for b in tool_uses])
                self._append("assistant", [{"type": "text", "text": "That got cut off."}])
                await say("Sorry, that got cut off. Could you ask again?")
                return

            results, held_message = [], ""
            for block in tool_uses:
                result.tools.append(block.name)
                if held_message:
                    results.append(self._tool_result(block.id, "Not run: another action is waiting for a yes.", True))
                    continue
                if block.name == "think_harder":
                    if self.ledger.state() is SpendState.NO_ESCALATION:
                        await say("I'm near this month's spending cap, so I'll keep this simple. ")
                        results.append(self._tool_result(block.id, "Unavailable near the spend cap; answer directly."))
                    else:
                        deep = True
                        results.append(self._tool_result(block.id, "A planner model will continue from here."))
                    continue
                outcome, held = await self._run_tool(block, channel, say)
                if held:
                    held_message = held
                results.append(outcome)

            self._append("user", results)
            if held_message:
                self._append("assistant", [{"type": "text", "text": held_message}])
                await say(held_message)
                result.awaiting_confirmation = True
                return

        await say("That took more steps than I expected, so I stopped.")
        self._append("assistant", [{"type": "text", "text": "Stopped: too many steps."}])

    def _repair_history(self) -> None:
        if not self.messages:
            return
        last = self.messages[-1]
        if last["role"] == "assistant":
            pending = [b["id"] for b in last["content"] if b["type"] == "tool_use"]
            if not pending:
                return
            self._append("user", [self._tool_result(i, "Interrupted by the user; not run.", True) for i in pending])
        self._append("assistant", [{"type": "text", "text": "(interrupted)"}])

    @staticmethod
    def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error:
            out["is_error"] = True
        return out

    async def _run_tool(self, block: Any, channel: Channel, say: OnText) -> tuple[dict[str, Any], str]:
        """Run one tool call through the gate. Returns (tool_result, held_message)."""
        tool = self.registry.get(block.name)
        if tool is None:
            return self._tool_result(block.id, f"Unknown tool {block.name}.", True), ""
        args = block.input
        if err := validate_input(args, tool.input_schema):
            return self._tool_result(block.id, f"INVALID_INPUT: {err}", True), ""

        try:
            # The read-back (or the phone's draft) can call the service too.
            decision = await self.gate.check(
                tool, args, channel, self.tainted, confirm_own=self.personality.confirm_own_actions
            )
        except ToolError as e:
            return self._tool_result(block.id, f"{tool.service or tool.name}: {e}", True), ""
        if decision.pending is not None:
            return self._tool_result(
                block.id, "Held for the user's spoken confirmation. The system is reading it back now; "
                          "do not repeat it or say it was done."), decision.message
        if not decision.allowed:
            content = decision.message
            if decision.parked_result is not None:
                content += f" ({decision.parked_result.content})"
                self._remember_undo(tool.name, decision.parked_result)
            return self._tool_result(block.id, content), ""

        if tool.cue and not self._turn_spoke:
            await say(tool.cue + " ")
        try:
            out = await tool.handler(args)
        except ToolError as e:
            return self._tool_result(block.id, f"{tool.service or tool.name}: {e}", True), ""
        except Exception as e:  # report, don't crash the conversation
            return self._tool_result(block.id, f"{tool.name} failed unexpectedly: {e}", True), ""
        res = out if isinstance(out, ActionResult) else ActionResult(str(out))
        if res.untrusted:
            self.tainted = True
        self._remember_undo(tool.name, res)
        return self._tool_result(block.id, res.content), ""

    def _remember_undo(self, name: str, res: ActionResult) -> None:
        if res.undo is not None:
            self.undo.push(res.undo_label or name, res.undo)
