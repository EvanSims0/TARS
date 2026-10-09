from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeBackend, reply, text, tool_use

from tars.actions import ActionResult, Channel, Tier, Tool, schema
from tars.gate import ConfirmationGate
from tars.integrations.gcal import meeting_tally
from tars.local_tools import personality_tool
from tars.mood import MoodMonitor, back_to_back, presenting_event
from tars.personality import Mood, Personality, PersonalitySettings

UTC = timezone.utc


def ev(start: datetime, minutes: int, summary: str = "Sync", guests: bool = True, rid: str | None = None):
    out = {"id": f"e{start:%H%M}{summary}", "summary": summary,
           "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat()}}
    if guests:
        out["attendees"] = [{"email": "me@x", "self": True}, {"email": "boss@x"}]
    if rid:
        out["recurringEventId"] = rid
    return out


# Settings and effective tone

def test_calm_presenting_and_stress_cap_humor(tmp_path):
    p = Personality.load(tmp_path / "p.json", PersonalitySettings(humor=75, bluntness=90))
    assert p.humor == 75 and p.name == "TARS"
    p.mood = Mood(stressed=True, stress_reason="back-to-back meetings")
    assert p.humor == 30 and "auto-reduced" in p.context()
    assert p.stress_notice() == "Humor reduced to 30 percent. You seem busy. "
    assert p.stress_notice() == ""  # said once per busy spell
    p.mood = Mood(presenting=True, presenting_reason="Zoom meeting")
    assert p.humor == 0 and "Discretion on" in p.context()
    p.mood = Mood()
    p.update(calm=True)
    assert p.name == "Vela" and p.humor == 0 and p.bluntness == 40


def test_settings_persist_and_clamp(tmp_path):
    p = Personality.load(tmp_path / "p.json", PersonalitySettings())
    assert p.update(humor=140, trust=60) == "humor 100%; trust 60%; I'll ask before changing anything in your accounts."
    again = Personality.load(tmp_path / "p.json", PersonalitySettings())
    assert again.settings.humor == 100 and again.confirm_own_actions


# Trust in the gate

async def _ok(args):
    return ActionResult("Added.")


ACCOUNT_TOOL = Tool("add_reminder", "x", schema({"content": {"type": "string"}}), Tier.CREATE_FOR_YOU, _ok,
                    service="Todoist", read_back=lambda a: f"Add a reminder: {a['content']}.")
LOCAL_TOOL = Tool("set_timer", "x", schema({}), Tier.CREATE_FOR_YOU, _ok)


async def test_low_trust_holds_account_changes_only():
    gate = ConfirmationGate()
    assert (await gate.check(ACCOUNT_TOOL, {"content": "milk"}, Channel.PC, False)).allowed
    held = await gate.check(ACCOUNT_TOOL, {"content": "milk"}, Channel.PC, False, confirm_own=True)
    assert not held.allowed and held.message == "Add a reminder: milk. Should I go ahead?"
    assert (await gate.check(LOCAL_TOOL, {}, Channel.PC, False, confirm_own=True)).allowed
    res = await gate.resolve("yes", Channel.PHONE)  # own actions can be confirmed from the phone
    assert res.executed and res.message == "Added."


async def test_no_to_own_action_says_cancelled():
    gate = ConfirmationGate()
    await gate.check(ACCOUNT_TOOL, {"content": "milk"}, Channel.PC, False, confirm_own=True)
    assert (await gate.resolve("no", Channel.PC)).message == "Okay, cancelled."


# The set_personality tool

async def test_lowering_trust_is_instant_raising_needs_a_yes(tmp_path):
    p = Personality.load(tmp_path / "p.json", PersonalitySettings())
    calls = []

    async def listener(personality):
        calls.append(personality.name)

    tool = personality_tool(p, [listener])
    gate = ConfirmationGate()
    assert (await gate.check(tool, {"trust": 60}, Channel.PC, False)).allowed
    res = await tool.handler({"trust": 60, "mode": "calm"})
    assert p.settings.trust == 60 and p.name == "Vela" and calls == ["Vela"]
    held = await gate.check(tool, {"trust": 90}, Channel.PC, False)
    assert not held.allowed and "Raise trust to 90%" in held.message
    assert await res.undo() == "Settings put back."
    assert p.settings.trust == 100 and p.name == "TARS"


async def test_agent_sends_personality_and_holds_when_trust_is_low(make_agent, spoken, registry, recorder):
    registry.tools["add_reminder"].service = "Todoist"
    backend = FakeBackend(reply(tool_use("add_reminder", {"content": "milk"})))
    agent = make_agent(backend)
    agent.personality.update(trust=60)
    agent.personality.mood = Mood(stressed=True, stress_reason="late-night deadline")
    await agent.handle("remind me to buy milk", spoken)
    first = backend.requests[0]["messages"][0]["content"][0]["text"]
    assert "TARS: humor 30%, bluntness 60%, trust 60%." in first
    said = "".join(spoken.out)
    assert said.startswith("Humor reduced to 30 percent.") and "Should I go ahead?" in said
    assert recorder.calls == []
    await agent.handle("yes", spoken)
    assert recorder.calls == [{"content": "milk"}]


# Discretion and stress

def test_presenting_and_back_to_back_from_calendar():
    now = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
    assert presenting_event([ev(now - timedelta(minutes=5), 30, "Q3 roadmap presentation")], now)
    assert not presenting_event([ev(now - timedelta(minutes=5), 30, "1:1")], now)
    chain = [ev(now + timedelta(minutes=m), 25, f"M{m}") for m in (0, 30, 60)]
    assert back_to_back(chain, now)
    spread = [ev(now + timedelta(minutes=m), 25, f"M{m}") for m in (0, 60, 120)]
    assert not back_to_back(spread, now)


async def test_mood_monitor_refresh():
    p = Personality()
    now = datetime(2026, 10, 9, 22, 30, tzinfo=UTC)

    async def no_events(start, end):
        return []

    async def two_open():
        return 2

    monitor = MoodMonitor(p, events=no_events, open_tasks_due_today=two_open,
                          detect_meeting=lambda: "", clock=lambda: now)
    mood = await monitor.refresh()
    assert mood.stressed and mood.stress_reason == "late-night deadline"
    monitor.detect_meeting = lambda: "Zoom meeting"
    assert (await monitor.refresh()).presenting and p.humor == 0


# Meeting cost tally

def test_meeting_tally_projects_recurring_cost():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(days=7)
    events = [ev(start + timedelta(days=d, hours=10), 60, "Weekly status sync", rid="sync") for d in (1,)]
    events += [ev(start + timedelta(days=2, hours=15), 30, "Vendor call")]
    events += [ev(start + timedelta(days=3, hours=9), 60, "Gym", guests=False)]  # not a meeting
    out = meeting_tally(events, start, end)
    assert out.startswith("2 meetings, 1.5 hours over 7 days")
    assert "Recurring 'Weekly status sync': 1.0 h in this window (1x), about 52 hours a year." in out


def test_mission_brief_in_instructions():
    from tars.config import Config

    config = Config()
    assert "pre-launch checklist" in config.instructions()
    config.brief.style = "plain"
    assert "pre-launch" not in config.instructions()
