"""The confirmation gate, enforced in code so the model cannot skip it.

Anything that affects other people is held here, read back in full, and only
runs after the user says an explicit yes on the PC. From the phone it is parked
as a draft and waits until the user is back at the PC.

When the user has lowered TARS's trust, its own account changes (reminders,
calendar holds, filing email) are held for a yes as well, from any channel.
"""

from __future__ import annotations

import inspect
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .actions import ActionResult, Channel, Tier, Tool, ToolError

_YES = {
    "yes", "yeah", "yep", "yup", "yes please", "sure", "confirm", "confirmed",
    "do it", "send it", "send", "go ahead", "go for it", "ok", "okay", "please do",
    "affirmative", "correct", "that's right", "yes send it", "yes do it",
    "yes go ahead", "yes that's right", "yeah send it", "yeah go ahead",
}
# Any of these anywhere means it is not a clean yes.
_HEDGES = re.compile(
    r"\b(no|not|don'?t|do not|wait|stop|cancel|hold|change|but|actually|instead|"
    r"never ?mind|later|maybe|edit|fix|wrong)\b"
)
_NO = re.compile(r"^(no|nope|nah|cancel|stop|don'?t|do not|never ?mind|forget it)\b")


def classify_reply(text: str) -> str:
    """Return "yes", "no" or "other" for a reply to a read-back."""
    norm = re.sub(r"[^\w\s']", " ", text.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return "other"
    if _NO.match(norm):
        return "no"
    if _HEDGES.search(norm):
        return "other"
    if norm in _YES:
        return "yes"
    return "other"


@dataclass
class PendingAction:
    tool_name: str
    args: dict[str, Any]
    read_back: str
    tainted: bool
    pc_only: bool = True
    affects_others: bool = True
    created: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class GateDecision:
    allowed: bool
    # Set when the action is held: the exact text to say to the user.
    message: str = ""
    pending: PendingAction | None = None
    parked_result: ActionResult | None = None


@dataclass
class Resolution:
    executed: bool
    message: str
    result: ActionResult | None = None
    # The reply was neither yes nor no; it should go on to the model as a new request.
    passthrough: bool = False


class ConfirmationGate:
    # A held action expires so a stray "yes" much later can't send it.
    PENDING_TTL_SECONDS = 120

    def __init__(self, parked_path: Path | None = None):
        self.pending: PendingAction | None = None
        self._tools: dict[str, Tool] = {}
        self._parked_path = parked_path

    async def check(
        self, tool: Tool, args: dict[str, Any], channel: Channel, tainted: bool,
        confirm_own: bool = False,
    ) -> GateDecision:
        try:
            tier = tool.effective_tier(args)
        except ValueError:
            return GateDecision(allowed=False, message="That isn't something I can do.")
        if tier is Tier.READ:
            return GateDecision(allowed=True)
        if tier is Tier.CREATE_FOR_YOU and not (confirm_own and tool.service):
            return GateDecision(allowed=True)

        read_back = tool.read_back(args) if tool.read_back else tool.describe(args)
        if inspect.isawaitable(read_back):
            read_back = await read_back
        if tier is not Tier.AFFECTS_OTHERS:
            # Low trust or a trust raise: ask on whichever channel the user is on.
            return self._hold(tool, args, read_back, tainted, pc_only=False, affects_others=False)
        if channel is Channel.PHONE:
            # Never sent from the phone: save as a draft and say what's waiting.
            parked = None
            if tool.park is not None:
                out = await tool.park(args)
                parked = out if isinstance(out, ActionResult) else ActionResult(str(out))
            self._park(PendingAction(tool.name, args, read_back, tainted))
            return GateDecision(
                allowed=False,
                message=f"Saved as a draft for when you're back at the PC: {read_back}",
                parked_result=parked,
            )

        return self._hold(tool, args, read_back, tainted, pc_only=True, affects_others=True)

    def _hold(self, tool: Tool, args: dict[str, Any], read_back: str, tainted: bool,
              pc_only: bool, affects_others: bool) -> GateDecision:
        self._tools[tool.name] = tool
        self.pending = PendingAction(tool.name, args, read_back, tainted, pc_only, affects_others)
        note = " This conversation has read email, so check it carefully." if tainted and affects_others else ""
        return GateDecision(
            allowed=False,
            message=f"{read_back}{note} Should I go ahead?",
            pending=self.pending,
        )

    def has_pending(self) -> bool:
        if self.pending and time.time() - self.pending.created > self.PENDING_TTL_SECONDS:
            self.pending = None
        return self.pending is not None

    def cancel(self) -> None:
        self.pending = None

    async def resolve(self, reply: str, channel: Channel) -> Resolution:
        """Handle the user's reply to a read-back."""
        if not self.has_pending():
            return Resolution(executed=False, message="", passthrough=True)
        pending = self.pending
        assert pending is not None
        verdict = classify_reply(reply)
        if verdict == "no":
            self.pending = None
            message = "Okay, cancelled. Nothing was sent." if pending.affects_others else "Okay, cancelled."
            return Resolution(executed=False, message=message)
        if verdict == "other":
            self.pending = None
            return Resolution(executed=False, message="", passthrough=True)
        if pending.pc_only and channel is not Channel.PC:
            return Resolution(executed=False, message="That needs a yes at the PC.")

        self.pending = None
        tool = self._tools[pending.tool_name]
        try:
            out = await tool.handler(pending.args)
        except ToolError as e:
            return Resolution(executed=False, message=f"That didn't go through: {e}")
        except Exception:
            return Resolution(executed=False, message="That didn't go through because of an error on my side.")
        result = out if isinstance(out, ActionResult) else ActionResult(str(out))
        return Resolution(executed=True, message=result.content, result=result)

    # Parked actions (from the phone) wait on disk until the user is at the PC.

    def _park(self, action: PendingAction) -> None:
        if self._parked_path is None:
            return
        items = self.parked()
        items.append(asdict(action))
        self._parked_path.parent.mkdir(parents=True, exist_ok=True)
        self._parked_path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def parked(self) -> list[dict[str, Any]]:
        if self._parked_path is None or not self._parked_path.exists():
            return []
        return json.loads(self._parked_path.read_text(encoding="utf-8"))

    def clear_parked(self) -> None:
        if self._parked_path is not None and self._parked_path.exists():
            self._parked_path.unlink()
