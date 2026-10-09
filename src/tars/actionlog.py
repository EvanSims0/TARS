"""What TARS did to your accounts, for the History window: each action, how it was approved,
and whether it can still be undone. Kept as long as transcripts."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_IDS = re.compile(r"\s*\[(?:id|draft) [^\]]*\]")


def clean_summary(text: str) -> str:
    """Tool results carry ids for the model; people don't need them."""
    return _IDS.sub("", text).strip()


class ActionLog:
    def __init__(self, data_dir: Path, keep_days: int = 7):
        self.dir = data_dir / "actions"
        self.keep_days = keep_days

    def _file(self, when: datetime | None = None) -> Path:
        return self.dir / f"{(when or datetime.now()):%Y-%m-%d}.jsonl"

    def _append(self, entry: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def record(self, tool: str, service: str, summary: str, how: str, undo_id: str | None = None,
               reversible: bool = True) -> str:
        entry_id = uuid.uuid4().hex[:10]
        self._append({"id": entry_id, "ts": time.time(), "tool": tool, "service": service,
                      "summary": clean_summary(summary), "how": how, "undo_id": undo_id, "reversible": reversible})
        return entry_id

    def mark_undone(self, entry_id: str) -> None:
        self._append({"undone": entry_id, "ts": time.time()})

    def recent(self, days: int | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        """Newest first, with an `undone` flag folded in."""
        now = now or datetime.now()
        rows, undone = [], set()
        # Oldest day first, so file order is write order; ties on the clock (Windows ticks every
        # ~15 ms) keep that order instead of looking simultaneous.
        for back in reversed(range(days or self.keep_days)):
            path = self._file(now - timedelta(days=back))
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                item = json.loads(line)
                if "undone" in item:
                    undone.add(item["undone"])
                else:
                    item["_seq"] = len(rows)
                    rows.append(item)
        for row in rows:
            row["undone"] = row["id"] in undone
        rows.sort(key=lambda r: (r["ts"], r.pop("_seq")), reverse=True)
        return rows

    def purge(self, now: datetime | None = None) -> None:
        if not self.dir.exists():
            return
        cutoff = (now or datetime.now()).date() - timedelta(days=self.keep_days)
        for path in self.dir.glob("*.jsonl"):
            try:
                if datetime.strptime(path.stem, "%Y-%m-%d").date() < cutoff:
                    path.unlink()
            except ValueError:
                continue
