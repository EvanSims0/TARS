"""The days the clocks change: early November (25 hours) and mid March (23 hours) in New York."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import respx

from tars.brief import local_day
from tars.integrations import gcal
from tars.integrations.google_auth import GoogleSession

NY = "America/New_York"


def _calendar_tools():
    s = GoogleSession({"refresh_token": "r", "client_id": "c", "client_secret": "s"})
    s._access, s._expires = "tok", 9e12
    return {t.name: t for t in gcal.build_tools(gcal.Calendar(s, NY))}


@respx.mock
@pytest.mark.parametrize("day, start, end", [
    ("2026-11-01", "2026-11-01T00:00:00-04:00", "2026-11-02T00:00:00-05:00"),  # clocks go back
    ("2026-03-08", "2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00"),  # clocks go forward
])
async def test_a_whole_day_on_a_clock_change_runs_midnight_to_midnight(day, start, end):
    route = respx.get(f"{gcal.CAL}/events").respond(json={"items": []})
    await _calendar_tools()["list_events"].handler({"start": day, "end": day})
    params = route.calls[0].request.url.params
    assert (params["timeMin"], params["timeMax"]) == (start, end)


@respx.mock
async def test_tomorrow_asked_the_evening_before_the_change():
    route = respx.get(f"{gcal.CAL}/events").respond(json={"items": [
        {"id": "e1", "summary": "Brunch", "start": {"dateTime": "2026-11-01T11:00:00-05:00"}}]})
    res = await _calendar_tools()["list_events"].handler({"start": "2026-11-01", "end": "2026-11-02"})
    params = route.calls[0].request.url.params
    assert params["timeMin"] == "2026-11-01T00:00:00-04:00" and params["timeMax"] == "2026-11-02T00:00:00-05:00"
    assert res.card["rows"] == [["11:00", "Brunch"]] and res.card["title"].startswith("SUN 01 NOV")


def test_times_on_the_changeover_get_the_right_offset():
    assert gcal.instant("2026-11-01T09:00", NY).isoformat() == "2026-11-01T09:00:00-05:00"
    assert gcal.instant("2026-03-08T09:00", NY).isoformat() == "2026-03-08T09:00:00-04:00"
    # 1:30 happens twice in November; the first one (still daylight time) is meant.
    assert gcal.instant("2026-11-01T01:30", NY).isoformat() == "2026-11-01T01:30:00-04:00"


def test_free_time_counts_the_extra_hour():
    start, end = gcal.instant("2026-11-01", NY), gcal.instant("2026-11-02", NY)
    assert (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)) == timedelta(hours=25)
    slots = gcal.free_slots([{"start": "2026-11-01T12:00:00-05:00", "end": "2026-11-01T13:00:00-05:00"}],
                            start, end, 60)
    assert [(a.isoformat(), b.isoformat()) for a, b in slots] == [
        ("2026-11-01T00:00:00-04:00", "2026-11-01T12:00:00-05:00"),
        ("2026-11-01T13:00:00-05:00", "2026-11-02T00:00:00-05:00"),
    ]


@pytest.mark.parametrize("now, hours", [
    (datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc), 25),  # 9:00 EST
    (datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc), 23),   # 9:00 EDT
    (datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc), 24),
])
def test_the_morning_brief_looks_at_the_whole_local_day(now, hours):
    start, end = local_day(now, NY)
    assert start.hour == 0 and end.hour == 0 and (end - start).days in (0, 1)
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=hours)
    assert start.date() == now.astimezone(start.tzinfo).date()


@pytest.mark.parametrize("clock, offset", [
    (datetime(2026, 10, 31, 9, 0), "UTC-04:00"),
    (datetime(2026, 11, 1, 9, 0), "UTC-05:00"),
    (datetime(2026, 3, 8, 9, 0), "UTC-04:00"),
])
async def test_the_model_is_told_the_offset_for_that_day(make_agent, spoken, clock, offset):
    from conftest import FakeBackend, reply, text

    backend = FakeBackend(reply(text("Nine.")))
    agent = make_agent(backend)
    agent.clock = lambda: clock
    await agent.handle("what time is it", spoken)
    assert f"({NY}, {offset})" in backend.requests[0]["messages"][0]["content"][0]["text"]
