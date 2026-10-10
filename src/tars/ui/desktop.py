"""The Windows desktop shell: tray icon and menu, the overlay, and the app windows.

It runs as its own small process (`python -m tars.ui.desktop`) so the native window loop never
blocks TARS's audio. It talks to TARS only through the local app server, with the same token.
Needs the `desktop` extra (pywebview, pystray); without it TARS keeps its console indicator.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL import Image

    from .server import AppServer

STATE_NAMES = {"idle": "Idle", "listening": "Listening", "thinking": "Thinking", "speaking": "Speaking",
               "confirm": "Needs confirmation", "muted": "Muted", "problem": "Problem"}
SHOWN = {"listening", "thinking", "speaking", "confirm", "problem"}
OVERLAY_W, OVERLAY_MARGIN = 396, 16  # the 380 px card plus room for its shadow
HIDE_AFTER = 4.0  # the overlay fades a few seconds after the conversation goes quiet


def available() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("webview", "pystray", "PIL"))


def launch(server: AppServer) -> subprocess.Popen | None:
    """Start the shell next to TARS; None when it isn't installed or this isn't Windows."""
    if sys.platform != "win32" or not available():
        return None
    base = f"http://127.0.0.1:{server.port}"
    data_dir = str(server.ctx.config.data_dir)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # The token goes through the environment, never the command line other programs can read.
    keep = str(server.ctx.config.privacy.transcript_days)
    return subprocess.Popen([sys.executable, "-m", "tars.ui.desktop", "--base", base, "--data", data_dir,
                             "--keep-days", keep],
                            env={**os.environ, "TARS_UI_TOKEN": server.token}, creationflags=flags)


# ---------- tray icons: one shape per state, drawn for the taskbar ----------

def render_glyph(state: str, size: int = 32) -> Image.Image:
    """The state's shape in white (the idle dot in slate), on a transparent square."""
    from PIL import Image, ImageDraw

    scale = 8  # draw large, then shrink for clean edges at 16 px
    s = size * scale / 24
    img = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white, slate = (255, 255, 255, 255), (112, 128, 144, 255)

    def circle(cx: float, cy: float, r: float, width: float = 0, fill: Any = None, outline: Any = white) -> None:
        box = [(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s]
        if width:
            d.ellipse(box, outline=outline, width=max(1, round(width * s)))
        else:
            d.ellipse(box, fill=fill or outline)

    def line(points: list[tuple[float, float]], width: float) -> None:
        d.line([(x * s, y * s) for x, y in points], fill=white, width=max(1, round(width * s)), joint="curve")

    if state == "idle":
        circle(12, 12, 4, fill=slate, outline=slate)
    elif state == "listening":
        circle(12, 12, 4.2, 2)
        circle(12, 12, 9.25, 1.6)
    elif state == "thinking":
        circle(12, 12, 7.5, 2, outline=(255, 255, 255, 90))
        d.arc([4.5 * s, 4.5 * s, 19.5 * s, 19.5 * s], -90, 0, fill=white, width=round(2.6 * s))
    elif state == "speaking":
        for x, h in ((3, 6), (7, 12), (11, 17), (15, 11), (19, 6)):
            d.rounded_rectangle([x * s, (12 - h / 2) * s, (x + 2) * s, (12 + h / 2) * s], radius=s, fill=white)
    elif state == "confirm":
        circle(12, 12, 9.25, 1.6)
        d.arc([9.4 * s, 6.6 * s, 14.6 * s, 11.8 * s], 180, 60, fill=white, width=round(2 * s))
        line([(12.9, 11.5), (12, 12.6), (12, 13.6)], 2)
        circle(12, 17, 1.2)
    elif state == "muted":
        d.rounded_rectangle([9 * s, 3 * s, 15 * s, 14 * s], radius=3 * s, outline=white, width=round(2 * s))
        d.arc([6 * s, 5 * s, 18 * s, 17 * s], 0, 180, fill=white, width=round(2 * s))
        line([(12, 17), (12, 20.5)], 2)
        line([(4, 4), (20, 20)], 2)
    elif state == "problem":
        d.polygon([(12 * s, 4 * s), (20.8 * s, 19.6 * s), (3.2 * s, 19.6 * s)], outline=white, width=round(2 * s))
        line([(12, 10), (12, 14)], 2)
        circle(12, 17, 1.2)
    return img.resize((size, size), Image.Resampling.LANCZOS)


# ---------- the shell process ----------

class Shell:
    def __init__(self, base: str, token: str, data_dir: Path):
        self.base, self.token, self.data_dir = base, token, data_dir
        self.state = "idle"
        self.live: dict[str, Any] = {}
        self.quiet_since = time.time()
        self.overlay = None
        self.windows: dict[str, Any] = {}
        self.icon = None
        self.overlay_visible = False

    def call(self, path: str, body: dict[str, Any] | None = None, timeout: float = 20) -> Any:
        req = urllib.request.Request(self.base + path, headers={"X-Tars-Token": self.token})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def page(self, name: str) -> str:
        return f"{self.base}/{name}?t={self.token}"

    # windows

    def open_window(self, name: str, title: str, width: int, height: int) -> None:
        import webview

        win = self.windows.get(name)
        if win is not None:
            try:
                win.restore()
                win.show()
                return
            except Exception:
                self.windows.pop(name, None)
        win = webview.create_window(title, self.page(name), width=width, height=height, min_size=(720, 520),
                                    frameless=True, easy_drag=False, background_color="#36454f",
                                    js_api=WindowApi(self, name))
        win.events.closed += lambda: self.windows.pop(name, None)
        self.windows[name] = win

    def _overlay_position(self) -> tuple[int, int]:
        import webview

        saved = self.data_dir / "overlay.json"
        try:
            pos = json.loads(saved.read_text(encoding="utf-8"))
            return int(pos["x"]), int(pos["y"])
        except (OSError, ValueError, KeyError):
            screen = webview.screens[0]
            return screen.width - OVERLAY_W - OVERLAY_MARGIN, screen.height - 560

    def remember_overlay_position(self) -> None:
        if self.overlay is None:
            return
        try:
            (self.data_dir / "overlay.json").write_text(json.dumps({"x": self.overlay.x, "y": self.overlay.y}),
                                                        encoding="utf-8")
        except OSError:
            pass

    # tray

    def tooltip(self) -> str:
        line = f"TARS · {STATE_NAMES.get(self.state, 'Idle')}"
        problem = self.live.get("problem") or {}
        if problem:
            return f"{line}\n{problem.get('service', 'A service')} is down"
        drafts = self.live.get("drafts") or 0
        return f"{line}\n{drafts} draft{'s' * (drafts != 1)} waiting" if drafts else line

    def build_tray(self) -> Any:
        import pystray

        def act(action: str):
            return lambda icon, item: threading.Thread(target=self._control, args=(action,), daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem("Talk", act("talk"), default=True),
            pystray.MenuItem("Mute", act("mute")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("History", lambda i, it: self.open_window("history", "TARS · History", 1120, 820)),
            pystray.MenuItem("Settings", lambda i, it: self.open_window("settings", "TARS · Settings", 1000, 820)),
            pystray.MenuItem("Memory", lambda i, it: self.open_window("memory", "TARS · Memory", 1120, 820)),
            pystray.MenuItem("Status", lambda i, it: self.open_window("status", "TARS · Status", 760, 600)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", act("quit")),
        )
        self.icon = pystray.Icon("TARS", render_glyph("idle", 32), "TARS · Idle", menu)
        return self.icon

    def _control(self, action: str) -> None:
        try:
            self.call("/api/control", {"action": action})
        except Exception:
            pass

    # the loop that follows TARS's state

    def follow(self) -> None:
        version = -1
        while True:
            try:
                live = self.call(f"/api/live?since={version}", timeout=20)
            except Exception:
                time.sleep(2)
                try:
                    self.call("/api/status", timeout=3)
                except Exception:
                    self.exit()  # TARS has stopped
                    return
                continue
            version = live.get("version", version)
            self.live = live
            self.apply(live.get("state", "idle"))

    def apply(self, state: str) -> None:
        if state != self.state and self.icon is not None:
            self.icon.icon = render_glyph(state, 32)
        self.state = state
        if self.icon is not None:
            self.icon.title = self.tooltip()
        if state in SHOWN:
            self.quiet_since = 0.0
            self.show_overlay()
        elif not self.quiet_since:
            self.quiet_since = time.time()

    def show_overlay(self) -> None:
        if self.overlay is not None and not self.overlay_visible:
            self.overlay.show()
            self.overlay_visible = True

    def tick(self) -> None:
        """Hide the overlay a few seconds after things go quiet."""
        while True:
            time.sleep(0.5)
            if self.overlay_visible and self.quiet_since and time.time() - self.quiet_since > HIDE_AFTER:
                self.remember_overlay_position()
                self.overlay.hide()
                self.overlay_visible = False

    def exit(self) -> None:
        import webview

        self.remember_overlay_position()
        if self.icon is not None:
            self.icon.stop()
        for win in list(webview.windows):
            win.destroy()

    def run(self) -> None:
        import webview

        x, y = self._overlay_position()
        self.overlay = webview.create_window(
            "TARS", self.page("overlay"), width=OVERLAY_W, height=520, x=x, y=y, frameless=True, easy_drag=True,
            on_top=True, focus=False, transparent=True, hidden=True, resizable=False, background_color="#36454f",
            js_api=WindowApi(self, "overlay"))
        self.build_tray().run_detached()
        threading.Thread(target=self.follow, daemon=True, name="follow").start()
        threading.Thread(target=self.tick, daemon=True, name="tick").start()
        webview.start(private_mode=True)


class WindowApi:
    """What a page can ask of its window: window.pywebview.api.<method>()."""

    def __init__(self, shell: Shell, name: str):
        self._shell, self._name = shell, name

    def _window(self) -> Any:
        return self._shell.overlay if self._name == "overlay" else self._shell.windows.get(self._name)

    def minimize(self) -> None:
        win = self._window()
        if win is not None:
            win.minimize()

    def close(self) -> None:
        if self._name == "overlay":
            self._shell.remember_overlay_position()
            self._shell.overlay.hide()
            self._shell.overlay_visible = False
            return
        win = self._window()
        if win is not None:
            win.destroy()

    def fit(self, height: int) -> None:
        """The overlay sizes itself to its card."""
        win = self._window()
        if win is not None and 80 < height < 900:
            win.resize(OVERLAY_W, int(height))

    def open(self, name: str) -> None:
        sizes = {"history": (1120, 820), "settings": (1000, 820), "memory": (1120, 820), "status": (760, 600),
                 "setup": (760, 640)}
        if name in sizes:
            self._shell.open_window(name, f"TARS · {name.capitalize()}", *sizes[name])


def main() -> None:
    parser = argparse.ArgumentParser(prog="tars-desktop")
    parser.add_argument("--base", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--keep-days", type=int, default=7)
    args = parser.parse_args()
    from .. import logs

    logs.start(Path(args.data), "desktop", args.keep_days)
    Shell(args.base, os.environ["TARS_UI_TOKEN"], Path(args.data)).run()


if __name__ == "__main__":
    main()
