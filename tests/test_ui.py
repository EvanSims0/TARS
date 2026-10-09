"""The desktop UI's backend: live state, action log, config editing, the app server and the shell's icons."""

from __future__ import annotations

import http.client
import json
import time
import tomllib
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from conftest import FakeBackend, reply, text, tool_use

from tars.actionlog import ActionLog, clean_summary
from tars.actions import ActionResult, Channel, Tier, Tool, ToolError, schema
from tars.config import Config
from tars.config_edit import set_values
from tars.gate import ConfirmationGate, PendingAction
from tars.ui.live import JOKE_MARK, LiveState

# ---------- live state ----------


def test_live_state_follows_a_turn_and_keeps_a_problem_until_the_next_turn():
    live = LiveState()
    live.turn_started("What's on today?")
    snap = live.snapshot()
    assert snap["state"] == "thinking" and snap["user_text"] == "What's on today?" and snap["user_final"]
    live.reply("Five things.")
    assert live.snapshot()["first_word_ms"] is not None
    live.failed("Google Calendar", "Google isn't reachable right now.")
    live.set_state("idle")  # the tray keeps saying "problem" until a turn succeeds
    assert live.snapshot()["state"] == "problem"
    assert live.snapshot()["problem"]["short"] == "GCAL"
    live.turn_started("try again")
    live.set_state("idle")
    assert live.snapshot()["state"] == "idle" and live.snapshot()["problem"] is None


def test_live_wait_returns_on_change():
    live = LiveState()
    version = live.snapshot()["version"]
    start = time.monotonic()
    assert live.wait(version, timeout=0.2)["version"] == version  # nothing changed: times out
    assert time.monotonic() - start >= 0.15
    live.set_state("listening")
    assert live.wait(version, timeout=5)["state"] == "listening"


# ---------- action log ----------


def test_action_log_records_hides_ids_and_folds_in_undos(tmp_path):
    log = ActionLog(tmp_path)
    first = log.record("add_reminder", "Todoist", "Added 'milk'. [id 123]", "done", "u1")
    log.record("send_email", "Gmail", "Sent to sam@x.com.", "you said yes", None, reversible=False)
    log.mark_undone(first)
    rows = log.recent()
    assert [r["tool"] for r in rows] == ["send_email", "add_reminder"]
    assert rows[1]["summary"] == "Added 'milk'." and rows[1]["undone"] and not rows[0]["reversible"]
    assert clean_summary("Draft saved to a@b.c: 'Hi'. [draft r-99]") == "Draft saved to a@b.c: 'Hi'."


# ---------- config editing ----------


def test_config_edit_keeps_comments_and_handles_new_and_empty_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('user_name = ""            # how TARS refers to you\n\n[voice]\n# input_device_index = 1\n'
                    'follow_up_seconds = 8.0\n\n[location]\n# latitude = 40.71\n', encoding="utf-8")
    set_values(path, {"user_name": "Evan", "voice.input_device_index": 3, "location.latitude": 37.77,
                      "email.urgent": ["a", 'b "q"'], "spend.monthly_cap_usd": 30})
    text_ = path.read_text(encoding="utf-8")
    assert 'user_name = "Evan"            # how TARS refers to you' in text_
    data = tomllib.loads(text_)
    assert data["voice"]["input_device_index"] == 3 and data["location"]["latitude"] == 37.77
    assert data["email"]["urgent"] == ["a", 'b "q"'] and data["spend"]["monthly_cap_usd"] == 30
    set_values(path, {"voice.input_device_index": None})  # "Windows default": the key goes away
    assert "input_device_index" not in tomllib.loads(path.read_text(encoding="utf-8"))["voice"]


# ---------- agent hooks ----------


async def test_joke_marker_lights_the_cue_and_is_never_spoken(make_agent, spoken):
    agent = make_agent(FakeBackend(reply(text(f"Done. {JOKE_MARK} Dana seemed relieved."))))
    agent.live = LiveState()
    await agent.handle("move my 3pm", spoken)
    assert JOKE_MARK not in "".join(spoken.out)
    snap = agent.live.snapshot()
    assert snap["joke"] and JOKE_MARK not in snap["reply_text"] and snap["state"] == "idle"


async def test_cards_failures_and_actions_reach_the_ui(make_agent, spoken, registry, tmp_path):
    async def events(args):
        return ActionResult("Dentist at 3pm", card={"kind": "list", "title": "TODAY · 1 ITEM", "rows": [["3:00", "Dentist"]]})

    async def broken(args):
        raise ToolError("Google isn't reachable right now.")

    registry.tools["list_events"].handler = events
    registry.tools["list_events"].service = "Google Calendar"
    backend = FakeBackend(reply(tool_use("list_events", {"day": "today"})), reply(text("Dentist at three.")),
                          reply(tool_use("add_reminder", {"content": "milk"}, id="tu_2")), reply(text("Added.")))
    agent = make_agent(backend)
    agent.live, agent.actions = LiveState(), ActionLog(tmp_path)
    await agent.handle("what's on today?", spoken)
    assert agent.live.snapshot()["card"]["rows"] == [["3:00", "Dentist"]]
    await agent.handle("remind me to buy milk", spoken)
    assert [a["tool"] for a in agent.actions.recent()] == ["add_reminder"]  # reads aren't logged

    registry.tools["list_events"].handler = broken
    agent.backend = FakeBackend(reply(tool_use("list_events", {"day": "today"}, id="tu_3")), reply(text("Calendar's down.")))
    await agent.handle("what's on today?", spoken)
    assert agent.live.snapshot()["state"] == "problem"


async def test_a_held_send_shows_its_card_and_yes_is_logged(make_agent, spoken, tmp_path):
    agent = make_agent(FakeBackend(reply(tool_use("send_email", {"to": ["sam@x.com"], "body": "Running late"}))))
    agent.live, agent.actions = LiveState(), ActionLog(tmp_path)
    await agent.handle("tell Sam I'm late", spoken)
    card = agent.live.snapshot()["confirm"]
    assert agent.live.snapshot()["state"] == "confirm"
    assert card["title"] == "Email to sam@x.com" and card["body"] == "Running late" and card["question"] == "Send exactly this?"
    await agent.handle("yes", spoken)
    assert agent.actions.recent()[0]["how"] == "you said yes"


# ---------- gate: parked drafts ----------


async def test_reviewing_a_parked_draft_and_saying_no_parks_it_again(tmp_path):
    gate = ConfirmationGate(tmp_path / "parked.json")
    sent = []

    async def send(args):
        sent.append(args)
        return "Sent."

    tool = Tool("send_email", "send", schema({}), Tier.AFFECTS_OTHERS, send, read_back=lambda a: "Email to Priya.")
    gate._park(PendingAction("send_email", {"to": ["priya@x.com"], "body": "Slides"}, "Email to Priya.", False))
    gate.review_parked(0, tool)
    assert gate.parked() == [] and gate.confirm_card()["title"] == "Email to priya@x.com"
    res = await gate.resolve("no", Channel.PC)
    assert res.message == "Okay. It's still saved as a draft." and len(gate.parked()) == 1 and sent == []
    gate.review_parked(0, tool)
    res = await gate.resolve("yes", Channel.PC)
    assert res.executed and sent and gate.parked() == []


# ---------- app server ----------


class Client:
    def __init__(self, url: str, token: str):
        u = urlparse(url)
        self.host, self.port, self.token = u.hostname, u.port, token

    def call(self, method, path, body=None, headers=None, token=True):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        h = {"Content-Type": "application/json"} if body is not None else {}
        if token:
            h["X-Tars-Token"] = self.token
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw or b"{}")
        except ValueError:
            return resp.status, raw


@pytest.fixture
def running(tmp_path, monkeypatch):
    import asyncio
    import threading

    from tars import secrets
    from tars.app import build_app

    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    config = Config(home=tmp_path)
    (tmp_path / "config.toml").write_text("[voice]\nfollow_up_seconds = 8.0\n", encoding="utf-8")

    async def announce(text_):
        pass

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    app = build_app(config, announce, backend=FakeBackend())
    submitted = []
    app.ui.ctx.loop, app.ui.ctx.submit = loop, submitted.append
    app.ui.start()
    yield app, Client(app.ui.url(), app.ui.token), submitted
    app.ui.stop()
    loop.call_soon_threadsafe(loop.stop)


def test_server_refuses_without_the_token_a_loopback_host_or_json(running):
    app, c, _ = running
    assert c.call("GET", "/api/status", token=False)[0] == 403
    assert c.call("GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert c.call("GET", f"/history?t={app.ui.token}")[0] == 200
    assert c.call("GET", "/history?t=nope")[0] == 403
    status, _ = c.call("POST", "/api/reply", headers={"Content-Type": "text/plain"}, body=None)
    assert status == 415  # a cross-site form can't post here


def test_live_status_and_reply(running):
    app, c, submitted = running
    app.live.set_state("listening")
    status, live = c.call("GET", "/api/live?since=-1")
    assert status == 200 and live["state"] == "listening" and live["telemetry"]["trust"] == 100
    assert c.call("POST", "/api/reply", {"text": "yes"})[0] == 200 and submitted == ["yes"]
    assert c.call("GET", "/api/status")[1]["running"]


def test_history_undo_and_forget(running):
    app, c, _ = running
    undone = []

    async def undo():
        undone.append(True)
        return "Removed it."

    undo_id = app.agent.undo.push("add task", undo)
    action = app.actions.record("add_reminder", "Todoist", "Added 'milk'.", "done", undo_id)
    app.transcripts.append("user", "remind me to buy milk")
    app.transcripts.append("assistant", "Added milk.")
    status, history = c.call("GET", "/api/history")
    assert status == 200 and history["actions"][0]["can_undo"]
    assert history["exchanges"][0] == {**history["exchanges"][0], "you": "remind me to buy milk", "tars": "Added milk."}
    status, body = c.call("POST", "/api/undo", {"id": action})
    assert status == 200 and body["message"] == "Removed it." and undone
    assert c.call("GET", "/api/history")[1]["actions"][0]["undone"]
    assert c.call("POST", "/api/history/forget", {"ts": history["exchanges"][0]["ts"]})[0] == 200
    assert c.call("GET", "/api/history")[1]["exchanges"] == []


def test_settings_save_to_config_and_apply_now(running, tmp_path):
    app, c, _ = running
    status, body = c.call("POST", "/api/settings", {"values": {"brief.time": "08:30", "voice.mute_key": "<ctrl>+<alt>+n"}})
    assert status == 200 and body["restart"] == ["voice.mute_key"]
    assert app.config.brief.time == "08:30"
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["brief"]["time"] == "08:30"
    assert c.call("POST", "/api/settings", {"values": {"brief.time": "noon"}})[0] == 400
    assert c.call("POST", "/api/settings", {"values": {"home": "/etc"}})[0] == 400  # only listed keys


def test_raising_trust_needs_an_explicit_confirmation(running):
    app, c, _ = running
    assert c.call("POST", "/api/personality", {"trust": 60})[0] == 200  # lowering is fine
    status, body = c.call("POST", "/api/personality", {"trust": 90})
    assert status == 409 and app.personality.settings.trust == 60
    assert c.call("POST", "/api/personality", {"trust": 90, "confirmed": True})[0] == 200
    assert app.personality.settings.trust == 90


def test_keys_are_checked_by_name_only(running):
    _, c, _ = running
    assert c.call("POST", "/api/accounts/key", {"name": "GOOGLE_OAUTH_TOKEN", "value": "x"})[0] == 400
    assert c.call("POST", "/api/accounts/key", {"name": "ANTHROPIC_API_KEY", "value": "has spaces"})[0] == 400


# ---------- the desktop shell's tray icons ----------


@pytest.mark.parametrize("state", ["idle", "listening", "thinking", "speaking", "confirm", "muted", "problem"])
def test_every_state_has_its_own_shape(state):
    pytest.importorskip("PIL")
    from tars.ui.desktop import render_glyph

    img = render_glyph(state, 32)
    assert img.size == (32, 32) and img.getbbox() is not None  # something is drawn
    idle = render_glyph("idle", 32)
    if state != "idle":
        assert img.tobytes() != idle.tobytes()


def test_shell_only_starts_on_windows_with_its_extras(monkeypatch):
    from tars.ui import desktop

    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert desktop.launch(SimpleNamespace()) is None


# ---------- mission brief card ----------


async def test_mission_card_rows_come_from_real_data():
    from datetime import datetime, timedelta, timezone

    from tars.brief import mission_card

    now = datetime(2026, 10, 9, 8, 48, tzinfo=timezone.utc)

    async def weather(args):
        return "home now: clear, 14°C (feels 13°C), wind 9 km/h.\n..."

    async def mail(args):
        return ActionResult("[id 1] ...\n[id 2] ...", untrusted=True)

    async def events(start, end):
        at = (now + timedelta(minutes=42)).isoformat()
        return [{"summary": "Sync", "start": {"dateTime": at}, "end": {"dateTime": at}},
                {"summary": "Dentist", "location": "Ferry Building, SF",
                 "start": {"dateTime": (now + timedelta(hours=6)).isoformat()}, "end": {"dateTime": at}}]

    tools = {"get_weather": SimpleNamespace(handler=weather), "list_email": SimpleNamespace(handler=mail)}
    app = SimpleNamespace(
        agent=SimpleNamespace(registry=SimpleNamespace(get=tools.get)), mood=SimpleNamespace(events=events),
        ledger=SimpleNamespace(month_total=lambda: 6.2), config=Config())
    card = await mission_card(app, now)
    assert card["title"] == "MISSION BRIEF · FRI 9 OCT" and card["lead"] == "T−42 min to first meeting"
    rows = {r["label"]: (r["text"], r["status"]) for r in card["rows"]}
    assert rows["WEATHER"] == ("Clear, 14°C", "GO")
    assert rows["CALENDAR"][0].startswith("2 items, first at")
    assert rows["INBOX"] == ("2 unread", "HOLD")
    assert rows["SPEND"] == ("$6.20 of $25 this month", "GO")
    assert rows["DENTIST"][0].endswith("Ferry Building")


def test_actions_logged_in_the_same_clock_tick_stay_newest_first(tmp_path, monkeypatch):
    import tars.actionlog

    monkeypatch.setattr(tars.actionlog.time, "time", lambda: 1000.0)  # Windows' clock ticks every ~15 ms
    log = ActionLog(tmp_path)
    log.record("first", "S", "one", "done")
    log.record("second", "S", "two", "done")
    assert [r["tool"] for r in log.recent()] == ["second", "first"]
