"""`tars check` against mocked services: no network, no spend."""

from __future__ import annotations

import httpx
import respx

from tars import check, secrets
from tars.config import Config
from tars.integrations import places, todoist


def _config() -> Config:
    config = Config()
    config.user_email = "me@x.com"
    config.voice.tts_voice_id = "v1"
    config.location.timezone = "America/New_York"
    config.location.latitude, config.location.longitude = 40.7, -74.0
    return config


def test_config_checks_catch_a_bad_timezone():
    config = _config()
    config.location.timezone = "Eastern"
    by_name = {c.name: c for c in check.check_config(config)}
    assert by_name["Timezone"].ok is False and "isn't a timezone" in by_name["Timezone"].detail
    assert by_name["Home location"].ok and by_name["Your email"].ok
    assert by_name["Home address"].ok is None  # optional


@respx.mock
async def test_service_checks_report_rejected_keys_and_missing_voice(monkeypatch):
    keys = {secrets.DEEPGRAM_API_KEY: "dg", secrets.ELEVENLABS_API_KEY: "el", secrets.TODOIST_API_TOKEN: "td"}
    monkeypatch.setattr(secrets, "get_secret", keys.get)
    respx.get(check.DEEPGRAM_PROJECTS).respond(401)
    respx.get(check.ELEVENLABS_VOICE.format(voice_id="v1")).respond(404)
    respx.get(f"{todoist.BASE_URL}/projects").respond(json={"results": [{"id": "1", "name": "Shopping"}]})
    respx.get(f"{todoist.BASE_URL}/tasks/filter").respond(json={"results": [{"id": "t", "content": "x"}]})
    http = httpx.AsyncClient()

    dg = await check.check_deepgram(http)
    assert dg.ok is False and "rejected" in dg.detail
    el = await check.check_elevenlabs(http, "v1")
    assert el.ok is False and "wasn't found" in el.detail
    td = await check.check_todoist(_config(), http)
    assert td.ok and td.detail == "1 projects, 1 task(s) due; 'Shopping' found"


@respx.mock
async def test_unexpected_response_shape_is_a_failure_not_a_crash(monkeypatch):
    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    respx.get(places.FORECAST).respond(json={"unexpected": True})
    results = await check.check_places(_config(), httpx.AsyncClient())
    weather = results[0]
    assert weather.name == "Weather" and weather.ok is False and "KeyError" in weather.detail
    assert results[1].ok is None  # Google Maps not set up is allowed


async def test_missing_keys_say_how_to_add_them(monkeypatch):
    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    claude = await check.check_claude(_config())
    assert claude.ok is False and "tars set-key ANTHROPIC_API_KEY" in claude.detail
    google = await check.check_google(_config(), httpx.AsyncClient())
    assert google[0].ok is False and "google-auth" in google[0].detail
