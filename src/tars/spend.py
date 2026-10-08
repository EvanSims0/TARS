"""Spend tracking and the monthly cap.

The cap covers everything billed by usage: Claude, Deepgram and ElevenLabs.
Near the cap TARS stops escalating to the deeper model and says so; at the cap
it stops calling paid services until the month rolls over.
"""

from __future__ import annotations

import enum
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import SpendConfig


@dataclass(frozen=True)
class ModelPrice:
    input: float  # USD per million tokens
    output: float
    long_input: float | None = None  # rate above the long-prompt threshold, if any
    long_output: float | None = None
    long_threshold: int = 100_000


# Checked against Anthropic pricing on 2026-10-07. Cache reads are 0.1x input, 5-minute writes 1.25x.
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(0.10, 0.50, long_input=0.50, long_output=2.50),
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00),
    "claude-opus-5-5": ModelPrice(4.00, 20.00),
}


def llm_cost(model: str, usage: Any) -> float:
    price = PRICES.get(model)
    if price is None:
        return 0.0
    uncached = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0
    in_rate, out_rate = price.input, price.output
    if price.long_input is not None and uncached + read + write > price.long_threshold:
        in_rate, out_rate = price.long_input, price.long_output or out_rate
    return (uncached * in_rate + read * in_rate * 0.1 + write * in_rate * 1.25 + output * out_rate) / 1e6


class SpendState(enum.Enum):
    OK = "ok"
    NO_ESCALATION = "no_escalation"
    CAPPED = "capped"


class SpendLedger:
    def __init__(self, data_dir: Path, config: SpendConfig):
        self.dir = data_dir
        self.config = config

    def _month_file(self, when: datetime | None = None) -> Path:
        when = when or datetime.now()
        return self.dir / f"spend-{when:%Y-%m}.jsonl"

    def _append(self, entry: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), **entry}
        with self._month_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def record_llm(self, model: str, usage: Any) -> float:
        usd = llm_cost(model, usage)
        self._append({
            "kind": "llm",
            "model": model,
            "usd": usd,
            "input": getattr(usage, "input_tokens", 0) or 0,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "output": getattr(usage, "output_tokens", 0) or 0,
        })
        return usd

    def record_stt(self, seconds: float) -> float:
        usd = seconds / 60 * self.config.deepgram_usd_per_minute
        self._append({"kind": "stt", "seconds": seconds, "usd": usd})
        return usd

    def record_tts(self, characters: int) -> float:
        usd = characters / 1000 * self.config.elevenlabs_usd_per_1k_chars
        self._append({"kind": "tts", "chars": characters, "usd": usd})
        return usd

    def _entries(self) -> list[dict[str, Any]]:
        path = self._month_file()
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def month_total(self) -> float:
        return sum(e["usd"] for e in self._entries())

    def today_total(self) -> float:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return sum(e["usd"] for e in self._entries() if e["ts"] >= start)

    def state(self) -> SpendState:
        total = self.month_total()
        cap = self.config.monthly_cap_usd
        if total >= cap:
            return SpendState.CAPPED
        if total >= cap * self.config.escalation_cutoff:
            return SpendState.NO_ESCALATION
        return SpendState.OK


class TurnLog:
    """Per-turn response time and cost, for /status and the phase-gate test script."""

    def __init__(self, data_dir: Path):
        self.dir = data_dir

    def _file(self, when: datetime | None = None) -> Path:
        return self.dir / f"turns-{(when or datetime.now()):%Y-%m-%d}.jsonl"

    def record(self, **entry: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._file().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **entry}) + "\n")

    def today(self) -> list[dict[str, Any]]:
        path = self._file()
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def summary(self) -> dict[str, Any]:
        turns = self.today()
        firsts = [t["first_word_ms"] for t in turns if t.get("first_word_ms") is not None]
        out: dict[str, Any] = {"turns": len(turns)}
        if firsts:
            firsts.sort()
            out["first_word_p50_ms"] = round(statistics.median(firsts))
            out["first_word_p90_ms"] = round(firsts[min(len(firsts) - 1, int(len(firsts) * 0.9))])
            out["under_3s_share"] = round(sum(f < 3000 for f in firsts) / len(firsts), 2)
        return out
