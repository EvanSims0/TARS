"""TARS's local app server: the overlay, History, Settings, Setup, Memory and Status pages.

It listens on 127.0.0.1 only. Every page and API call needs the random token from the URL TARS
opened (or, for API calls, the X-Tars-Token header) and a loopback Host header, so other web
pages can't read or change anything through it. Anything that touches your accounts runs on
TARS's own event loop, the same way a spoken request does.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from loguru import logger

from .. import secrets as keys
from ..config import Config, _merge
from ..config_edit import set_values
from ..memory import Vault
from .live import LiveState

if TYPE_CHECKING:
    from ..app import App

HERE = Path(__file__).parent
PAGES = {"overlay", "history", "settings", "setup", "memory", "status"}
STATIC = {"tars.css": "text/css; charset=utf-8", "tars.js": "text/javascript; charset=utf-8"}

# Settings people can change from the window: dotted config key -> type check.
EDITABLE: dict[str, Callable[[Any], bool]] = {
    "user_name": lambda v: isinstance(v, str) and len(v) < 80,
    "user_email": lambda v: isinstance(v, str) and len(v) < 200,
    "voice.push_to_talk_key": lambda v: isinstance(v, str) and 0 < len(v) < 60,
    "voice.mute_key": lambda v: isinstance(v, str) and 0 < len(v) < 60,
    "voice.follow_up_seconds": lambda v: isinstance(v, (int, float)) and 3 <= v <= 30,
    "voice.live_transcript": lambda v: isinstance(v, bool),
    "voice.calm_voice_id": lambda v: isinstance(v, str) and len(v) < 80,
    "voice.input_device_index": lambda v: v is None or (isinstance(v, int) and 0 <= v < 512),
    "voice.output_device_index": lambda v: v is None or (isinstance(v, int) and 0 <= v < 512),
    "brief.enabled": lambda v: isinstance(v, bool),
    "brief.time": lambda v: isinstance(v, str) and len(v) == 5 and v[2] == ":",
    "brief.style": lambda v: v in ("mission", "plain"),
    "brief.contents": lambda v: isinstance(v, str) and len(v) < 400,
    "alerts.max_per_day": lambda v: isinstance(v, int) and 0 <= v <= 20,
    "alerts.quiet_start": lambda v: isinstance(v, str) and len(v) == 5 and v[2] == ":",
    "alerts.quiet_end": lambda v: isinstance(v, str) and len(v) == 5 and v[2] == ":",
    "email.urgent": lambda v: isinstance(v, list) and all(isinstance(x, str) and len(x) < 300 for x in v),
    "email.ignore": lambda v: isinstance(v, list) and all(isinstance(x, str) and len(x) < 120 for x in v),
    "spend.monthly_cap_usd": lambda v: isinstance(v, (int, float)) and 1 <= v <= 1000,
    "spend.escalation_cutoff": lambda v: isinstance(v, (int, float)) and 0.1 <= v <= 1,
    "privacy.transcript_days": lambda v: isinstance(v, int) and 1 <= v <= 90,
    "privacy.backup_days": lambda v: isinstance(v, int) and 1 <= v <= 365,
    "location.home_address": lambda v: isinstance(v, str) and len(v) < 300,
    "location.latitude": lambda v: v is None or (isinstance(v, (int, float)) and -90 <= v <= 90),
    "location.longitude": lambda v: v is None or (isinstance(v, (int, float)) and -180 <= v <= 180),
    "location.timezone": lambda v: isinstance(v, str) and len(v) < 60,
}
# Keys that take effect only when TARS restarts (hotkeys and audio devices are bound at start).
RESTART = {"voice.push_to_talk_key", "voice.mute_key", "voice.input_device_index", "voice.output_device_index",
           "voice.calm_voice_id"}
SERVICE_KEYS = {
    keys.ANTHROPIC_API_KEY: "Claude", keys.DEEPGRAM_API_KEY: "Deepgram", keys.ELEVENLABS_API_KEY: "ElevenLabs",
    keys.TODOIST_API_TOKEN: "Todoist", keys.GOOGLE_MAPS_API_KEY: "Google Maps",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class UiContext:
    config: Config
    vault: Vault
    app: App | None = None
    live: LiveState | None = None
    # TARS's event loop; actions on accounts and the agent run there.
    loop: asyncio.AbstractEventLoop | None = None
    # A typed turn, handled exactly like a spoken one ("yes", "give me my morning brief").
    submit: Callable[[str], None] | None = None
    # Tray and overlay buttons: "talk", "mute", "quit", "dismiss".
    controls: dict[str, Callable[[], None]] = field(default_factory=dict)
    config_path: Path | None = None
    google_status: dict[str, str] = field(default_factory=lambda: {"state": "idle", "message": ""})


class AppServer:
    def __init__(self, ctx: UiContext, open_browser: Callable[[str], Any] = webbrowser.open):
        self.ctx = ctx
        self.token = token_urlsafe(18)
        self._open_browser = open_browser
        self._server: ThreadingHTTPServer | None = None

    # Lifecycle

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("not started")
        return self._server.server_address[1]

    def url(self, page: str = "status") -> str:
        return f"http://127.0.0.1:{self.port}/{page}?t={self.token}"

    def start(self) -> str:
        if self._server is None:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
            self._server.daemon_threads = True
            threading.Thread(target=self._server.serve_forever, daemon=True, name="tars-ui").start()
        return self.url()

    def open(self, page: str = "status") -> str:
        self.start()
        url = self.url(page)
        self._open_browser(url)
        return url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # Helpers

    def _on_loop(self, coro: Awaitable[Any], timeout: float = 60) -> Any:
        """Run a coroutine on TARS's loop (or a private one when TARS isn't running) and wait."""
        if self.ctx.loop is not None and self.ctx.loop.is_running():
            future: Future = asyncio.run_coroutine_threadsafe(coro, self.ctx.loop)  # type: ignore[arg-type]
            return future.result(timeout)
        return asyncio.run(coro)  # type: ignore[arg-type]

    def _need_app(self) -> App:
        if self.ctx.app is None:
            raise ApiError(503, "TARS isn't running. Start it with tars voice or tars chat.")
        return self.ctx.app

    def _save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            check = EDITABLE.get(key)
            if check is None or not check(value):
                raise ApiError(400, f"{key} can't be set to that.")
        path = self.ctx.config_path or self.ctx.config.home / "config.toml"
        set_values(path, values)
        nested: dict[str, Any] = {}
        for key, value in values.items():
            section, _, name = key.rpartition(".")
            (nested.setdefault(section, {}) if section else nested)[name] = value
        _merge(self.ctx.config, nested)
        app = self.ctx.app
        if app is not None:
            app.agent.instructions = self.ctx.config.instructions()
            app.agent.user_name, app.agent.user_email = self.ctx.config.user_name, self.ctx.config.user_email
            app.agent.timezone = self.ctx.config.location.timezone
            app.transcripts.keep_days = self.ctx.config.privacy.transcript_days
        restart = sorted(k for k in values if k in RESTART)
        return {"saved": list(values), "restart": restart}

    # Data for each page

    def live(self, since: int) -> dict[str, Any]:
        live = self.ctx.live
        snap = live.wait(since, timeout=12) if live is not None else {"state": "idle", "version": 0}
        app = self.ctx.app
        p = app.personality if app else None
        snap["telemetry"] = {"humor": p.humor if p else 0, "bluntness": p.bluntness if p else 0,
                             "trust": p.settings.trust if p else 0, "name": p.name if p else "TARS"}
        snap["prefs"] = {"live_transcript": self.ctx.config.voice.live_transcript,
                         "cue_light": p.settings.cue_light if p else True,
                         "talk_key": self.ctx.config.voice.push_to_talk_key}
        snap["now"] = time.time()
        snap["running"] = app is not None
        snap["drafts"] = len(app.agent.gate.parked()) if app else 0
        return snap

    def history(self) -> dict[str, Any]:
        app = self._need_app()
        days = self.ctx.config.privacy.transcript_days
        actions = app.actions.recent(days)
        for a in actions:
            a["can_undo"] = bool(a.get("undo_id")) and not a["undone"] and app.agent.undo.has(a["undo_id"])
        summary = app.turn_log.summary()
        drafts = [{"index": i, "read_back": d.get("read_back", ""), "tool": d.get("tool_name", ""),
                   "to": d.get("args", {}).get("to", []), "subject": d.get("args", {}).get("subject", ""),
                   "body": d.get("args", {}).get("body", ""), "created": d.get("created", 0)}
                  for i, d in enumerate(app.agent.gate.parked())]
        return {
            "exchanges": app.transcripts.exchanges(days),
            "actions": actions,
            "drafts": drafts,
            "today": {**summary, "spend_today": round(app.ledger.today_total(), 4),
                      "spend_month": round(app.ledger.month_total(), 4),
                      "cap": self.ctx.config.spend.monthly_cap_usd},
            "keep_days": days,
        }

    def settings(self) -> dict[str, Any]:
        c = self.ctx.config
        app = self.ctx.app
        p = app.personality.settings if app else None
        defaults = c.personality
        return {
            "personality": {"humor": p.humor if p else defaults.humor, "bluntness": p.bluntness if p else defaults.bluntness,
                            "trust": p.trust if p else defaults.trust, "calm": p.calm if p else False,
                            "cue_light": p.cue_light if p else True},
            "voice": {"push_to_talk_key": c.voice.push_to_talk_key, "mute_key": c.voice.mute_key,
                      "follow_up_seconds": c.voice.follow_up_seconds, "live_transcript": c.voice.live_transcript,
                      "tts_voice_id": c.voice.tts_voice_id, "calm_voice_id": c.voice.calm_voice_id,
                      "input_device_index": c.voice.input_device_index, "output_device_index": c.voice.output_device_index},
            "brief": {"enabled": c.brief.enabled, "time": c.brief.time, "style": c.brief.style, "contents": c.brief.contents},
            "alerts": {"max_per_day": c.alerts.max_per_day, "quiet_start": c.alerts.quiet_start, "quiet_end": c.alerts.quiet_end},
            "email": {"urgent": list(c.email.urgent), "ignore": list(c.email.ignore)},
            "spend": self.spend(),
            "privacy": {"transcript_days": c.privacy.transcript_days, "backup_days": c.privacy.backup_days,
                        "home": str(c.home)},
            "accounts": self.accounts(),
            "running": app is not None,
        }

    def spend(self) -> dict[str, Any]:
        c = self.ctx.config.spend
        app = self.ctx.app
        by_kind = {"llm": 0.0, "stt": 0.0, "tts": 0.0}
        month = today = 0.0
        if app is not None:
            for entry in app.ledger._entries():
                by_kind[entry.get("kind", "llm")] = by_kind.get(entry.get("kind", "llm"), 0.0) + entry["usd"]
            month, today = app.ledger.month_total(), app.ledger.today_total()
        return {"cap": c.monthly_cap_usd, "cutoff": c.escalation_cutoff, "month": round(month, 4), "today": round(today, 4),
                "by_service": {"Claude": round(by_kind["llm"], 4), "Deepgram": round(by_kind["stt"], 4),
                               "ElevenLabs": round(by_kind["tts"], 4)}}

    def accounts(self) -> list[dict[str, Any]]:
        out = [{"key": name, "service": service, "set": bool(keys.get_secret(name)),
                "optional": name == keys.GOOGLE_MAPS_API_KEY} for name, service in SERVICE_KEYS.items()]
        out.insert(3, {"key": "GOOGLE", "service": "Google (Gmail, Calendar)",
                       "set": bool(keys.get_secret(keys.GOOGLE_OAUTH_TOKEN)), "optional": False})
        loc = self.ctx.config.location
        out.append({"key": "WEATHER", "service": "Weather (Open-Meteo)", "set": loc.latitude is not None,
                    "optional": False, "no_key": True})
        return out

    def check_service(self, service: str) -> dict[str, Any]:
        import httpx

        from .. import check

        async def run() -> list[Any]:
            async with httpx.AsyncClient(timeout=15) as http:
                if service == "Claude":
                    return [await check.check_claude(self.ctx.config)]
                if service == "Deepgram":
                    return [await check.check_deepgram(http)]
                if service == "ElevenLabs":
                    return [await check.check_elevenlabs(http, self.ctx.config.voice.tts_voice_id)]
                if service == "Todoist":
                    return [await check.check_todoist(self.ctx.config, http)]
                if service.startswith("Google ("):
                    return await check.check_google(self.ctx.config, http)
                if service in ("Weather (Open-Meteo)", "Google Maps"):
                    return await check.check_places(self.ctx.config, http)
                raise ApiError(400, "Unknown service.")

        results = asyncio.run(run())
        return {"results": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results]}

    def check_all(self) -> dict[str, Any]:
        from ..check import run_checks

        results = asyncio.run(run_checks(self.ctx.config))
        return {"results": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results]}

    def set_key(self, name: str, value: str) -> dict[str, Any]:
        if name not in SERVICE_KEYS:
            raise ApiError(400, "Unknown key.")
        value = value.strip()
        if not value or len(value) > 500 or any(c.isspace() for c in value):
            raise ApiError(400, "That doesn't look like a key; paste it again.")
        keys.set_secret(name, value)
        return self.check_service(SERVICE_KEYS[name])

    def google_connect(self, client_config: dict[str, Any] | None) -> dict[str, Any]:
        """Start Google sign-in in the background; the page polls google_status."""
        from ..integrations.google_auth import SCOPES

        if client_config is None:  # reconnect with the client already stored
            stored = keys.get_secret(keys.GOOGLE_OAUTH_TOKEN)
            if not stored:
                raise ApiError(400, "Choose the OAuth client file you downloaded from Google Cloud.")
            info = json.loads(stored)
            client_config = {"installed": {"client_id": info["client_id"], "client_secret": info["client_secret"],
                                           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                           "token_uri": "https://oauth2.googleapis.com/token",
                                           "redirect_uris": ["http://localhost"]}}
        if "installed" not in client_config:
            raise ApiError(400, "That file isn't a Desktop app OAuth client. Download it again as Desktop app.")
        status = self.ctx.google_status
        if status["state"] == "waiting":
            return status

        def flow() -> None:
            from google_auth_oauthlib.flow import InstalledAppFlow

            try:
                creds = InstalledAppFlow.from_client_config(client_config, SCOPES).run_local_server(
                    port=0, prompt="consent", access_type="offline", timeout_seconds=300)
                if not creds.refresh_token:
                    raise RuntimeError("Google didn't return a refresh token")
                keys.set_secret(keys.GOOGLE_OAUTH_TOKEN, json.dumps({
                    "refresh_token": creds.refresh_token, "client_id": creds.client_id,
                    "client_secret": creds.client_secret}))
                status.update(state="done", message="Google connected.")
            except Exception as e:  # shown on the page; the user can try again
                status.update(state="failed", message=f"Sign-in didn't finish: {e}")

        status.update(state="waiting", message="Finish signing in in the browser window that opened.")
        threading.Thread(target=flow, daemon=True, name="google-signin").start()
        return status

    def geocode(self, place: str) -> dict[str, Any]:
        import httpx

        from ..integrations.places import GEOCODE

        try:
            resp = httpx.get(GEOCODE, params={"name": place, "count": 1}, timeout=10)
            found = (resp.json().get("results") or [])
        except (httpx.HTTPError, ValueError) as e:
            raise ApiError(502, "The place lookup isn't reachable; type the numbers instead.") from e
        if not found:
            raise ApiError(404, f"Couldn't find {place}. Try just the town or city.")
        top = found[0]
        return {"latitude": round(top["latitude"], 4), "longitude": round(top["longitude"], 4),
                "timezone": top.get("timezone", ""), "name": ", ".join(x for x in (top.get("name"), top.get("admin1"),
                                                                                   top.get("country")) if x)}

    def devices(self) -> dict[str, Any]:
        try:
            import pyaudio
        except ImportError:
            return {"inputs": [], "outputs": [], "available": False}
        pa = pyaudio.PyAudio()
        try:
            ins, outs = [], []
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                entry = {"index": i, "name": info["name"]}
                if info["maxInputChannels"]:
                    ins.append(entry)
                if info["maxOutputChannels"]:
                    outs.append(entry)
            return {"inputs": ins, "outputs": outs, "available": True}
        finally:
            pa.terminate()

    def startup_tasks(self, enable: bool) -> dict[str, Any]:
        if sys.platform != "win32":
            raise ApiError(400, "Start at login is set up on Windows.")
        script = Path(__file__).resolve().parents[3] / "scripts" / "install-windows-tasks.ps1"
        if not enable:
            for name in ("TARS", "TARS vault backup"):
                subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)
            return {"enabled": False}
        if not script.exists():
            raise ApiError(500, "The start-up script isn't in this install.")
        done = subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise ApiError(500, (done.stderr or done.stdout).strip().splitlines()[-1] if (done.stderr or done.stdout)
                           else "The start-up script failed.")
        return {"enabled": True}

    def status(self) -> dict[str, Any]:
        app = self.ctx.app
        live = self.ctx.live.snapshot() if self.ctx.live else {"state": "idle"}
        data: dict[str, Any] = {"state": live["state"], "problem": live.get("problem"), "running": app is not None,
                                "accounts": self.accounts()}
        if app is not None:
            data["connected"] = app.connected
            data["today"] = {**app.turn_log.summary(), "spend_today": round(app.ledger.today_total(), 4),
                             "spend_month": round(app.ledger.month_total(), 4), "cap": self.ctx.config.spend.monthly_cap_usd}
            data["drafts"] = len(app.agent.gate.parked())
            data["timers"] = {k: round(v) for k, v in app.timers.remaining().items()}
            p = app.personality
            data["personality"] = {"name": p.name, "humor": p.humor, "bluntness": p.bluntness, "trust": p.settings.trust}
        return data

    # HTTP

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _host_ok(self) -> bool:
                port = srv._server.server_address[1] if srv._server else 0
                return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

            def _token_ok(self, token: str | None) -> bool:
                return self._host_ok() and token is not None and compare_digest(token, srv.token)

            def _send(self, status: int, body: bytes, kind: str, cache: bool = False) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                 "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the page moved on while a long-poll was waiting

            def _json(self, status: int, data: Any) -> None:
                self._send(status, json.dumps(data, default=str).encode("utf-8"), "application/json")

            def _body(self) -> dict[str, Any]:
                length = min(int(self.headers.get("Content-Length", "0") or 0), 200_000)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw or b"{}")
                except ValueError as e:
                    raise ApiError(400, "Bad request.") from e
                if not isinstance(data, dict):
                    raise ApiError(400, "Bad request.")
                return data

            def do_GET(self) -> None:
                url = urlparse(self.path)
                path = url.path.strip("/") or "status"
                if path == "favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if path.startswith("static/"):
                    name = path[7:]
                    if not self._host_ok() or name not in STATIC:
                        return self._send(404, b"", "text/plain")
                    return self._send(200, (HERE / "static" / name).read_bytes(), STATIC[name], cache=True)
                if path in PAGES:
                    if not self._token_ok(parse_qs(url.query).get("t", [None])[0]):
                        return self._send(403, b"Open this from TARS.", "text/plain")
                    page = (HERE / "pages" / f"{path}.html").read_text(encoding="utf-8").replace("__TOKEN__", srv.token)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if not path.startswith("api/"):
                    return self._json(404, {"error": "Not found."})
                if not self._token_ok(self.headers.get("X-Tars-Token")):
                    return self._json(403, {"error": "Forbidden."})
                self._api("GET", path[4:], parse_qs(url.query), {})

            def do_POST(self) -> None:
                path = urlparse(self.path).path.strip("/")
                if not path.startswith("api/") or not self._token_ok(self.headers.get("X-Tars-Token")):
                    return self._json(403, {"error": "Forbidden."})
                if "application/json" not in self.headers.get("Content-Type", ""):
                    return self._json(415, {"error": "Send JSON."})  # also blocks simple cross-site form posts
                try:
                    body = self._body()
                except ApiError as e:
                    return self._json(e.status, {"error": str(e)})
                self._api("POST", path[4:], {}, body)

            def _api(self, method: str, name: str, query: dict[str, list[str]], body: dict[str, Any]) -> None:
                try:
                    route = ROUTES.get((method, name))
                    if route is None:
                        return self._json(404, {"error": "Not found."})
                    self._json(200, route(srv, query, body))
                except ApiError as e:
                    self._json(e.status, {"error": str(e)})
                except Exception as e:  # keep the page usable; the log has the detail
                    logger.exception(f"ui api {name} failed")
                    self._json(500, {"error": f"Something went wrong: {e}"})

        return Handler


# ---------- routes ----------

def _app_action(fn: Callable[[App, dict[str, Any]], Awaitable[Any]]):
    def route(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
        app = srv._need_app()
        return srv._on_loop(fn(app, body))
    return route


async def _undo(app: App, body: dict[str, Any]) -> dict[str, Any]:
    from ..actions import ToolError

    action = next((a for a in app.actions.recent() if a["id"] == body.get("id")), None)
    if action is None or not action.get("undo_id"):
        raise ApiError(404, "That can't be undone.")
    try:
        message = await app.agent.undo.pop(action["undo_id"])
    except ToolError as e:
        raise ApiError(409, str(e)) from e
    app.actions.mark_undone(action["id"])
    return {"message": message}


async def _review_draft(app: App, body: dict[str, Any]) -> dict[str, Any]:
    items = app.agent.gate.parked()
    index = body.get("index")
    if not isinstance(index, int) or not 0 <= index < len(items):
        raise ApiError(404, "That draft is gone.")
    tool = app.agent.registry.get(items[index]["tool_name"])
    if tool is None:
        raise ApiError(409, "That service isn't connected right now.")
    app.agent.gate.review_parked(index, tool)
    if app.live is not None:
        app.live.held(app.agent.gate.confirm_card(tainted=False))
    if app.show_overlay:
        app.show_overlay()
    return {"held": True}


def _submit(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    text = str(body.get("text", "")).strip()
    if not text or len(text) > 2000:
        raise ApiError(400, "Nothing to send.")
    if srv.ctx.submit is None:
        raise ApiError(503, "TARS isn't listening right now.")
    srv.ctx.submit(text)
    return {"ok": True}


def _control(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    action = body.get("action")
    if action == "dismiss" and srv.ctx.live is not None:
        srv.ctx.live.clear_problem()
        return {"ok": True}
    fn = srv.ctx.controls.get(str(action))
    if fn is None:
        raise ApiError(503, "That isn't available right now.")
    fn()
    return {"ok": True}


def _personality(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    app = srv._need_app()
    p = app.personality
    values = {k: body[k] for k in ("humor", "bluntness", "trust") if isinstance(body.get(k), int)}
    if "trust" in values and values["trust"] > p.settings.trust and not body.get("confirmed"):
        raise ApiError(409, "Raising trust needs your confirmation.")
    calm = body.get("calm") if isinstance(body.get("calm"), bool) else None
    summary = p.update(calm=calm, **values)
    if isinstance(body.get("cue_light"), bool):
        p.settings.cue_light = body["cue_light"]
        p.save()

    async def notify() -> None:
        for listener in app.personality_listeners:
            await listener(p)

    if calm is not None and app.personality_listeners:
        srv._on_loop(notify())
    return {"summary": summary}


def _forget_exchange(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    app = srv._need_app()
    if not isinstance(body.get("ts"), (int, float)) or not app.transcripts.forget_exchange(body["ts"]):
        raise ApiError(404, "That conversation is already gone.")
    return {"ok": True}


def _clear_transcripts(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    app = srv._need_app()
    if body.get("confirm") is not True:
        raise ApiError(400, "Confirm first.")
    return {"removed": app.transcripts.clear()}


def _setup_state(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    c = srv.ctx.config
    path = srv.ctx.config_path or c.home / "config.toml"
    return {"you": {"user_name": c.user_name, "user_email": c.user_email, "home_address": c.location.home_address,
                    "latitude": c.location.latitude, "longitude": c.location.longitude, "timezone": c.location.timezone},
            "keys": {name: bool(keys.get_secret(name)) for name in SERVICE_KEYS},
            "google": bool(keys.get_secret(keys.GOOGLE_OAUTH_TOKEN)), "google_status": srv.ctx.google_status,
            "voice": {"input": c.voice.input_device_index, "output": c.voice.output_device_index},
            "windows": sys.platform == "win32", "config_exists": path.exists(), "config_path": str(path)}


def _save(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        raise ApiError(400, "Nothing to save.")
    path = srv.ctx.config_path or srv.ctx.config.home / "config.toml"
    if not path.exists():
        from ..cli import EXAMPLE_CONFIG

        path.parent.mkdir(parents=True, exist_ok=True)
        if EXAMPLE_CONFIG.exists():
            path.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return srv._save_settings(values)


def _google(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    client = body.get("client")
    if client is not None and not isinstance(client, dict):
        raise ApiError(400, "That file isn't an OAuth client.")
    return srv.google_connect(client)


def _memory(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    from ..memory_map import memory_data

    return memory_data(srv.ctx.vault)


def _forget_fact(srv: AppServer, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    from ..memory_map import fact_id

    fact = next((f for f in srv.ctx.vault.facts() if fact_id(f) == body.get("id")), None)
    if fact is None or not srv.ctx.vault.remove_line(fact.line):
        raise ApiError(404, "That fact is already gone.")
    return {"forgot": fact.text}


ROUTES: dict[tuple[str, str], Callable[[AppServer, dict[str, list[str]], dict[str, Any]], Any]] = {
    ("GET", "live"): lambda s, q, b: s.live(int((q.get("since") or ["-1"])[0] or -1)),
    ("GET", "status"): lambda s, q, b: s.status(),
    ("GET", "history"): lambda s, q, b: s.history(),
    ("GET", "settings"): lambda s, q, b: s.settings(),
    ("GET", "memory"): _memory,
    ("GET", "devices"): lambda s, q, b: s.devices(),
    ("GET", "setup"): _setup_state,
    ("GET", "check"): lambda s, q, b: s.check_all(),
    ("POST", "reply"): _submit,
    ("POST", "control"): _control,
    ("POST", "undo"): _app_action(_undo),
    ("POST", "drafts/review"): _app_action(_review_draft),
    ("POST", "forget"): _forget_fact,
    ("POST", "history/forget"): _forget_exchange,
    ("POST", "privacy/clear"): _clear_transcripts,
    ("POST", "personality"): _personality,
    ("POST", "settings"): _save,
    ("POST", "accounts/check"): lambda s, q, b: s.check_service(str(b.get("service", ""))),
    ("POST", "accounts/key"): lambda s, q, b: s.set_key(str(b.get("name", "")), str(b.get("value", ""))),
    ("POST", "accounts/google"): _google,
    ("POST", "geocode"): lambda s, q, b: s.geocode(str(b.get("place", ""))[:120]),
    ("POST", "startup"): lambda s, q, b: s.startup_tasks(bool(b.get("enable"))),
}

__all__ = ["AppServer", "UiContext", "ApiError"]
