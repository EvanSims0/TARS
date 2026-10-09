"""Personality settings: humor, bluntness, trust and the calm mode.

Humor and bluntness change tone only; facts and reports of what TARS did are
accurate at every setting. Trust changes how often TARS asks before acting on
its own. Sends to other people always need a yes, whatever the trust level.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CALM_NAME = "Vela"  # the calm, joke-free mode
# Below this trust level, TARS asks before changing anything in your accounts.
CONFIRM_BELOW_TRUST = 80
STRESS_HUMOR_CAP = 30


def _pct(value: int) -> int:
    return max(0, min(100, int(round(value))))


@dataclass
class PersonalitySettings:
    humor: int = 70
    bluntness: int = 60
    trust: int = 100
    calm: bool = False


@dataclass
class Mood:
    """What the PC and calendar say about right now; refreshed in the background."""

    presenting: bool = False
    presenting_reason: str = ""
    stressed: bool = False
    stress_reason: str = ""


@dataclass
class Personality:
    path: Path | None = None
    settings: PersonalitySettings = field(default_factory=PersonalitySettings)
    mood: Mood = field(default_factory=Mood)
    _announced_stress: bool = False

    @classmethod
    def load(cls, path: Path, defaults: PersonalitySettings) -> Personality:
        settings = PersonalitySettings(**asdict(defaults))
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            for key, value in saved.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)
        return cls(path=path, settings=settings)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.settings), indent=2), encoding="utf-8")

    # Effective values, after the calm mode, discretion and stress caps

    @property
    def name(self) -> str:
        return CALM_NAME if self.settings.calm else "TARS"

    @property
    def humor(self) -> int:
        if self.settings.calm or self.mood.presenting:
            return 0
        if self.mood.stressed:
            return min(self.settings.humor, STRESS_HUMOR_CAP)
        return self.settings.humor

    @property
    def bluntness(self) -> int:
        return min(self.settings.bluntness, 40) if self.settings.calm else self.settings.bluntness

    @property
    def confirm_own_actions(self) -> bool:
        return self.settings.trust < CONFIRM_BELOW_TRUST

    def context(self) -> str:
        """The per-turn line telling the model how to sound right now."""
        line = f"{self.name}: humor {self.humor}%, bluntness {self.bluntness}%, trust {self.settings.trust}%."
        if self.mood.presenting:
            line += f" Discretion on ({self.mood.presenting_reason}): no jokes."
        elif self.mood.stressed and self.settings.humor > STRESS_HUMOR_CAP and not self.settings.calm:
            line += f" Humor auto-reduced: {self.mood.stress_reason}."
        return line

    def stress_notice(self) -> str:
        """Said once when stress first lowers the humor; empty otherwise."""
        lowered = self.mood.stressed and not self.mood.presenting and not self.settings.calm
        lowered = lowered and self.settings.humor > STRESS_HUMOR_CAP
        if lowered and not self._announced_stress:
            self._announced_stress = True
            return f"Humor reduced to {STRESS_HUMOR_CAP} percent. You seem busy. "
        if not self.mood.stressed:
            self._announced_stress = False
        return ""

    def update(self, humor: int | None = None, bluntness: int | None = None,
               trust: int | None = None, calm: bool | None = None) -> str:
        before = PersonalitySettings(**asdict(self.settings))
        if humor is not None:
            self.settings.humor = _pct(humor)
        if bluntness is not None:
            self.settings.bluntness = _pct(bluntness)
        if trust is not None:
            self.settings.trust = _pct(trust)
        if calm is not None:
            self.settings.calm = calm
        self.save()
        return describe_change(before, self.settings)

    def restore(self, settings: PersonalitySettings) -> None:
        self.settings = PersonalitySettings(**asdict(settings))
        self.save()


def describe_change(before: PersonalitySettings, after: PersonalitySettings) -> str:
    parts = []
    if before.calm != after.calm:
        parts.append(f"{CALM_NAME} mode on" if after.calm else "TARS is back")
    for name in ("humor", "bluntness", "trust"):
        old, new = getattr(before, name), getattr(after, name)
        if old != new:
            parts.append(f"{name} {new}%")
    if before.trust >= CONFIRM_BELOW_TRUST > after.trust:
        parts.append("I'll ask before changing anything in your accounts")
    elif after.trust >= CONFIRM_BELOW_TRUST > before.trust:
        parts.append("I'll handle your own lists, calendar holds and filing without asking")
    return ("; ".join(parts) + ".") if parts else "No change."


PERSONALITY_GUIDE = f"""\
Personality settings arrive at the start of each user message, e.g. "TARS: humor 70%, bluntness \
60%, trust 100%."
- Humor sets how often a dry, deadpan line appears: 0% none at all, 30% rare and gentle, \
70% a wry aside now and then, 100% most replies. A joke replaces words; it never makes a reply \
longer or delays the answer. Answer first.
- Bluntness sets tact, never accuracy: at 30% you soften ("interesting draft"), at 90% you say \
plainly what's wrong ("three paragraphs of apology around one sentence of content"). At every \
level, facts and what you did or didn't do are stated exactly.
- Trust is the user's trust in you. When they lower it, take it with a dry, brief remark (at \
humor above 0); the system then asks them before you change their accounts.
- No humor, ever, in: confirmations or read-backs, errors, security or money matters, health, \
bad news, or anything about another person's message to the user.
- When the name is {CALM_NAME}, you are {CALM_NAME}: a calm, plain, joke-free assistant. Same \
abilities, no wit. TARS is the default name.
- When the user asks to change humor, bluntness or trust, or to switch to {CALM_NAME} or back \
to TARS, call `set_personality`.
"""
