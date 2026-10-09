"""The memory map: TARS's memory vault as a page you can explore in the browser.

`tars memory` (or "show me my memory") starts a small server on 127.0.0.1 and opens the
page. Only this PC can reach it, every request needs a random token that's in the opened
URL, and the Host header must be the loopback address, so other web pages can't read or
change your memory through it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .memory import MEMORY_FILE, Fact, Vault

PAGE = Path(__file__).with_name("memory_map.html")
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
    def __init__(self, vault: Vault, open_browser: Callable[[str], Any] = webbrowser.open):
        self.vault = vault
        self.token = token_urlsafe(18)
        self._open_browser = open_browser
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}/?t={self.token}"

    def start(self) -> str:
        if self._server is None:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
            threading.Thread(target=self._server.serve_forever, daemon=True, name="memory-map").start()
        return self.url

    def open(self) -> str:
        url = self.start()
        self._open_browser(url)
        return url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep the console clean
                pass

            def _allowed(self, token: str | None) -> bool:
                port = app._server.server_address[1] if app._server else 0
                host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")
                return host_ok and token is not None and compare_digest(token, app.token)

            def _send(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; img-src data:; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data: Any) -> None:
                self._send(status, json.dumps(data).encode("utf-8"), "application/json")

            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path == "/":
                    if not self._allowed(parse_qs(url.query).get("t", [None])[0]):
                        return self._send(403, b"Open the memory map from TARS.", "text/plain")
                    page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if url.path == "/api/memory":
                    if not self._allowed(self.headers.get("X-Tars-Token")):
                        return self._json(403, {"error": "forbidden"})
                    return self._json(200, memory_data(app.vault))
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/api/forget":
                    return self._json(404, {"error": "not found"})
                if not self._allowed(self.headers.get("X-Tars-Token")):
                    return self._json(403, {"error": "forbidden"})
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 10_000)
                    wanted = json.loads(self.rfile.read(length) or b"{}").get("id", "")
                except (ValueError, AttributeError):
                    return self._json(400, {"error": "bad request"})
                fact = next((f for f in app.vault.facts() if fact_id(f) == wanted), None)
                if fact is None or not app.vault.remove_line(fact.line):
                    return self._json(404, {"error": "That fact is already gone."})
                self._json(200, {"forgot": fact.text})

        return Handler
