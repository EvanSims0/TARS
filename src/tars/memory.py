"""Long-term memory kept as plain Markdown in TARS's own Obsidian vault.

Facts are small appends under known headings, so the file stays readable and
editable in Obsidian. The vault is not synced; a nightly copy is the backup.
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

HEADINGS = ["People", "Preferences", "Places", "Health", "Routines", "Other"]
MEMORY_FILE = "Memory.md"
_STAMP = re.compile(r"\s*\(\d{4}-\d{2}-\d{2}\)\s*$")
_DATE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)\s*$")


@dataclass
class Fact:
    category: str
    text: str
    saved: str  # YYYY-MM-DD, or "" for a line added by hand without a date
    line: str  # the exact line in Memory.md


class Vault:
    def __init__(self, root: Path):
        self.root = root
        self.file = root / MEMORY_FILE
        self._added: list[str] = []  # lines added this session, for "forget that"
        # The memory map's server thread edits the vault too.
        self._lock = threading.RLock()

    def ensure(self) -> None:
        if self.file.exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".obsidian").mkdir(exist_ok=True)
        body = "# TARS memory\n\nThings TARS has been told. Edit freely.\n"
        for heading in HEADINGS:
            body += f"\n## {heading}\n"
        self.file.write_text(body, encoding="utf-8")

    def _lines(self) -> list[str]:
        self.ensure()
        return self.file.read_text(encoding="utf-8").splitlines()

    def _write(self, lines: list[str]) -> None:
        self.file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def remember(self, fact: str, category: str = "Other") -> str:
        with self._lock:
            return self._remember(fact, category)

    def _remember(self, fact: str, category: str) -> str:
        fact = " ".join(fact.split())
        if not fact:
            raise ValueError("Nothing to remember")
        heading = next((h for h in HEADINGS if h.lower() == category.lower()), "Other")
        lines = self._lines()
        if any(_STAMP.sub("", line[2:]).lower() == fact.lower() for line in lines if line.startswith("- ")):
            return "Already noted."
        entry = f"- {fact} ({date.today().isoformat()})"
        wanted = f"## {heading}".lower()
        idx = next((i for i, line in enumerate(lines) if line.strip().lower() == wanted), None)
        if idx is None:  # the heading was removed by hand in Obsidian
            lines += ["", f"## {heading}"]
            idx = len(lines) - 1
        insert_at = idx + 1
        while insert_at < len(lines) and not lines[insert_at].startswith("## "):
            insert_at += 1
        # Keep entries directly under the heading, before any trailing blank line.
        while insert_at > idx + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, entry)
        self._write(lines)
        self._added.append(entry)
        return "Noted."

    def forget(self, query: str | None = None) -> list[str]:
        """Remove facts matching ``query``; with no query, the last fact added this session."""
        with self._lock:
            return self._forget(query)

    def _forget(self, query: str | None) -> list[str]:
        lines = self._lines()
        query = (query or "").strip()
        if query:
            # Whole words only, so "forget Al" can't also wipe every fact mentioning "always".
            pattern = re.compile(rf"(?<!\w){re.escape(query)}(?!\w)", re.I)
            removed = [line for line in lines if line.startswith("- ") and pattern.search(line[2:])]
        else:
            removed = [self._added[-1]] if self._added and self._added[-1] in lines else []
        if removed:
            self._write([line for line in lines if line not in removed])
            self._added = [a for a in self._added if a not in removed]
        return [_STAMP.sub("", r[2:]) for r in removed]

    def facts(self) -> list[Fact]:
        """Every remembered fact with its heading, in file order."""
        out, heading = [], "Other"
        for line in self._lines():
            if line.startswith("## "):
                heading = line[3:].strip() or "Other"
            elif line.startswith("- ") and line[2:].strip():
                saved = _DATE.search(line)
                out.append(Fact(heading, _STAMP.sub("", line[2:]).strip(), saved.group(1) if saved else "", line))
        return out

    def remove_line(self, line: str) -> bool:
        """Remove one exact fact line (the memory map's "forget"); False if it's no longer there."""
        with self._lock:
            lines = self._lines()
            if line not in lines or not line.startswith("- "):
                return False
            lines.remove(line)
            self._write(lines)
            self._added = [a for a in self._added if a != line]
            return True

    def snapshot(self) -> str:
        """The facts, grouped by heading, for the model's context."""
        out: list[str] = []
        for line in self._lines():
            if line.startswith("## ") or line.startswith("- "):
                out.append(line)
        return "\n".join(out)

    def backup(self, backup_root: Path, keep_days: int = 30, today: date | None = None) -> Path:
        today = today or date.today()
        target = backup_root / today.isoformat()
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(self.root, target)
        cutoff = today - timedelta(days=keep_days)
        for child in backup_root.iterdir():
            try:
                stamp = datetime.strptime(child.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if stamp < cutoff:
                shutil.rmtree(child)
        return target
