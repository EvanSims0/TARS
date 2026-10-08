"""Conversation transcripts: text only, kept on the PC for a few days.

No raw audio is ever stored. "Forget that" removes the latest exchange.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path


class Transcripts:
    def __init__(self, data_dir: Path, keep_days: int = 7):
        self.dir = data_dir / "transcripts"
        self.keep_days = keep_days

    def _file(self, when: datetime | None = None) -> Path:
        return self.dir / f"{(when or datetime.now()):%Y-%m-%d}.jsonl"

    def append(self, role: str, text: str, channel: str = "pc") -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._file().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "role": role, "channel": channel, "text": text}) + "\n")

    def recent(self, limit: int = 50) -> list[dict]:
        path = self._file()
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:] if line]

    def forget_last_exchange(self) -> int:
        """Drop the most recent user line and everything after it from today's file."""
        path = self._file()
        if not path.exists():
            return 0
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        last_user = max((i for i, r in enumerate(rows) if r["role"] == "user"), default=None)
        if last_user is None:
            return 0
        kept = rows[:last_user]
        path.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
        return len(rows) - len(kept)

    def purge(self, now: datetime | None = None) -> int:
        if not self.dir.exists():
            return 0
        cutoff = (now or datetime.now()).date() - timedelta(days=self.keep_days)
        removed = 0
        for path in self.dir.glob("*.jsonl"):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                path.unlink()
                removed += 1
        return removed
