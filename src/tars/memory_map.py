"""The memory map: TARS's memory vault as a page you can explore in the browser.

`tars memory` (or "show me my memory") opens the memory page of TARS's local app server
(`tars.ui.server`), which only this PC can reach. This module holds the data behind the page.
"""

from __future__ import annotations

import hashlib
import re
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from .memory import MEMORY_FILE, Fact, Vault

_NAME = re.compile(r"\b[A-Z][a-zA-Z]+(?:-[A-Z][a-zA-Z]+)?\b")
# Capitalised words that aren't people or places.
_NOT_NAMES = {
    "I", "TARS", "Vela", "The", "A", "An", "My", "Our", "Their", "His", "Her", "She", "He", "They", "We",
    "You", "It", "This", "That", "On", "In", "At", "Every", "Always", "Never", "No", "Not", "OK",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December",
}


def fact_id(fact: Fact) -> str:
    return hashlib.sha1(fact.line.encode("utf-8")).hexdigest()[:12]


def mentions(facts: list[Fact]) -> dict[str, list[str]]:
    """Names that appear in two or more facts, so the map can link them.

    A capitalised first word only counts if the same word also appears capitalised
    mid-sentence somewhere, which keeps "Prefers oat milk" from making "Prefers" a name.
    """
    mid: set[str] = set()
    found: list[tuple[str, set[str]]] = []
    for fact in facts:
        names = set()
        for match in _NAME.finditer(fact.text):
            word = match.group(0)
            if word in _NOT_NAMES:
                continue
            if match.start() > 0:
                mid.add(word)
            names.add(word)
        found.append((fact_id(fact), names))
    linked: dict[str, list[str]] = {}
    for fid, names in found:
        for name in names:
            if name in mid:
                linked.setdefault(name, []).append(fid)
    return {name: ids for name, ids in sorted(linked.items()) if len(ids) >= 2}


def memory_data(vault: Vault) -> dict[str, Any]:
    facts = vault.facts()
    categories: dict[str, list[dict[str, str]]] = {}
    for fact in facts:
        categories.setdefault(fact.category, []).append(
            {"id": fact_id(fact), "text": fact.text, "saved": fact.saved})
    notes = []
    for path in sorted(vault.root.glob("*.md")):
        if path.name != MEMORY_FILE:
            text = path.read_text(encoding="utf-8", errors="replace")
            notes.append({"title": path.stem, "preview": " ".join(text.split())[:240]})
    return {
        "categories": [{"name": name, "facts": items} for name, items in categories.items()],
        "mentions": mentions(facts),
        "notes": notes,
        "obsidian": "obsidian://open?path=" + quote(str(vault.file.resolve())),
        "count": len(facts),
    }


class MemoryMap:
    """The memory page of TARS's app server, for `tars memory` on its own."""

    def __init__(self, vault: Vault, open_browser: Callable[[str], Any] = webbrowser.open, config: Any = None):
        from .config import Config
        from .ui.server import AppServer, UiContext

        self.server = AppServer(UiContext(config or Config(), vault), open_browser)

    @property
    def token(self) -> str:
        return self.server.token

    @property
    def url(self) -> str:
        return self.server.url("memory")

    def start(self) -> str:
        self.server.start()
        return self.url

    def open(self) -> str:
        return self.server.open("memory")

    def stop(self) -> None:
        self.server.stop()
