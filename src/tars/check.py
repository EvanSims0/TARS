"""`tars check`: one read-only call to every account and setting, for the first run on the PC.

Nothing is created, changed or sent. The Claude check makes one tiny request (a fraction
of a cent) and reports its time to first word.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import secrets
from .actions import ToolError
from .config import Config

DEEPGRAM_PROJECTS = "https://api.deepgram.com/v1/projects"
ELEVENLABS_VOICE = "https://api.elevenlabs.io/v1/voices/{voice_id}"


@dataclass
class Check:
    name: str
    ok: bool | None  # None: not set up, which is allowed
    detail: str

    def line(self) -> str:
        mark = {True: "ok  ", False: "FAIL", None: "skip"}[self.ok]
        return f"  {mark}  {self.name}: {self.detail}"


def check_config(config: Config) -> list[Check]:
    out = []
    loc = config.location
    if loc.timezone:
        try:
            ZoneInfo(loc.timezone)
            out.append(Check("Timezone", True, loc.timezone))
        except (ZoneInfoNotFoundError, ValueError):
            out.append(Check("Timezone", False, f"{loc.timezone!r} isn't a timezone name, e.g. America/New_York"))
    else:
        out.append(Check("Timezone", False, "set location.timezone, e.g. America/New_York"))
    if loc.latitude is None or loc.longitude is None:
        out.append(Check("Home location", False, "set location.latitude and location.longitude for the weather"))
    else:
        out.append(Check("Home location", True, f"{loc.latitude}, {loc.longitude}"))
    out.append(Check("Your email", bool(config.user_email),
                     config.user_email or "set user_email so \"email me\" works"))
    out.append(Check("Home address", bool(loc.home_address) or None,
                     loc.home_address or "not set; only leave-by times need it"))
    return out


async def check_claude(config: Config) -> Check:
    key = secrets.get_secret(secrets.ANTHROPIC_API_KEY)
    if not key:
        return Check("Claude", False, "run `tars set-key ANTHROPIC_API_KEY`")
    import anthropic

    model = config.brain.fast_model
    start, first = time.monotonic(), None
    try:
        async with (
            anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=20) as client,
            client.messages.stream(
                model=model, max_tokens=16, thinking={"type": "disabled"},
                output_config={"effort": config.brain.fast_effort},
                messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            ) as stream,
        ):
            async for text in stream.text_stream:
                if first is None and text.strip():
                    first = time.monotonic() - start
            await stream.get_final_message()
    except anthropic.AuthenticationError:
        return Check("Claude", False, "the API key was rejected")
    except anthropic.PermissionDeniedError:
        return Check("Claude", False, "the key can't use this model; check the console's limits and credit")
    except anthropic.NotFoundError:
        return Check("Claude", False, f"model {model} wasn't found; check brain.fast_model")
    except anthropic.RateLimitError:
        return Check("Claude", False, "rate limited or out of credit; check the console")
    except anthropic.APIStatusError as e:
        return Check("Claude", False, f"error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        return Check("Claude", False, "couldn't reach the API; check the internet connection")
    ms = round((first or (time.monotonic() - start)) * 1000)
    return Check("Claude", True, f"{model}, first word in {ms} ms (budget 400-700 ms)")


async def check_deepgram(http: httpx.AsyncClient) -> Check:
    key = secrets.get_secret(secrets.DEEPGRAM_API_KEY)
    if not key:
        return Check("Deepgram", False, "run `tars set-key DEEPGRAM_API_KEY`")
    try:
        resp = await http.get(DEEPGRAM_PROJECTS, headers={"Authorization": f"Token {key}"})
    except httpx.HTTPError:
        return Check("Deepgram", False, "couldn't reach Deepgram")
    if resp.status_code in (401, 403):
        return Check("Deepgram", False, "the API key was rejected")
    if resp.status_code >= 400:
        return Check("Deepgram", False, f"error {resp.status_code}")
    return Check("Deepgram", True, "key accepted")


async def check_elevenlabs(http: httpx.AsyncClient, voice_id: str) -> Check:
    key = secrets.get_secret(secrets.ELEVENLABS_API_KEY)
    if not key:
        return Check("ElevenLabs", False, "run `tars set-key ELEVENLABS_API_KEY`")
    if not voice_id:
        return Check("ElevenLabs", False, "set voice.tts_voice_id in config.toml")
    try:
        resp = await http.get(ELEVENLABS_VOICE.format(voice_id=voice_id), headers={"xi-api-key": key})
    except httpx.HTTPError:
        return Check("ElevenLabs", False, "couldn't reach ElevenLabs")
    if resp.status_code == 401:
        return Check("ElevenLabs", False, "the API key was rejected")
    if resp.status_code in (400, 404):
        return Check("ElevenLabs", False, f"voice {voice_id} wasn't found in this account")
    if resp.status_code >= 400:
        return Check("ElevenLabs", False, f"error {resp.status_code}")
    return Check("ElevenLabs", True, f"voice '{resp.json().get('name', voice_id)}'")


async def _service(name: str, call: Callable[[], Awaitable[str]]) -> Check:
    try:
        return Check(name, True, await call())
    except ToolError as e:
        return Check(name, False, str(e))
    except Exception as e:  # a wrong field name or endpoint shows up here
        return Check(name, False, f"unexpected {type(e).__name__}: {e}")


async def check_google(config: Config, http: httpx.AsyncClient) -> list[Check]:
    from .integrations import gcal, gmail
    from .integrations.google_auth import GoogleSession

    session = GoogleSession.from_store(http)
    if session is None:
        return [Check("Google", False, "run `tars google-auth path\\to\\client_secret.json`")]
    calendar = gcal.Calendar(session, config.location.timezone)

    async def events() -> str:
        zone = gcal._zone(config.location.timezone)
        today = (datetime.now(zone) if zone else datetime.now()).date()
        start = gcal.instant(today.isoformat(), config.location.timezone)
        items = await calendar.events(start.isoformat(), (start + timedelta(days=1)).isoformat())
        return f"{len(items)} event(s) today"

    async def mail() -> str:
        found = await gmail.Gmail(session).search("is:unread in:inbox", 1)
        return "inbox readable" + (", unread mail waiting" if found else ", nothing unread")

    return [await _service("Google Calendar", events), await _service("Gmail", mail)]


async def check_todoist(config: Config, http: httpx.AsyncClient) -> Check:
    token = secrets.get_secret(secrets.TODOIST_API_TOKEN)
    if not token:
        return Check("Todoist", False, "run `tars set-key TODOIST_API_TOKEN`")
    from .integrations.todoist import TodoistClient

    client = TodoistClient(token, http)

    async def call() -> str:
        names = [p["name"] for p in await client.projects()]
        due = len(await client.tasks("today | overdue"))
        shop = config.todoist.shopping_project
        has_shop = any(n.lower() == shop.lower() for n in names)
        return (f"{len(names)} projects, {due} task(s) due; "
                + (f"'{shop}' found" if has_shop else f"'{shop}' will be created on first use"))

    return await _service("Todoist", call)


async def check_places(config: Config, http: httpx.AsyncClient) -> list[Check]:
    from .integrations.places import Places

    key = secrets.get_secret(secrets.GOOGLE_MAPS_API_KEY)
    places = Places(config.location, key, http)
    out = []
    if config.location.latitude is not None and config.location.longitude is not None:
        out.append(await _service("Weather", lambda: _first_line(places.weather(None, 1))))
    if key:
        out.append(Check("Google Maps", None, "key set; tested by asking for a leave-by time"))
    else:
        out.append(Check("Google Maps", None, "not set up, so leave-by times are off (test 3)"))
    return out


async def _first_line(text: Awaitable[str]) -> str:
    return (await text).splitlines()[0]


async def run_checks(config: Config) -> list[Check]:
    async with httpx.AsyncClient(timeout=15) as http:
        return await _run_checks(config, http)


async def _run_checks(config: Config, http: httpx.AsyncClient) -> list[Check]:
    results = check_config(config)
    results.append(await check_claude(config))
    results.append(await check_deepgram(http))
    results.append(await check_elevenlabs(http, config.voice.tts_voice_id))
    results += await check_google(config, http)
    results.append(await check_todoist(config, http))
    results += await check_places(config, http)
    return results


def audio_devices() -> list[str]:
    """Input and output devices, for voice.input_device_index and voice.output_device_index."""
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        default_in = pa.get_default_input_device_info().get("index") if pa.get_device_count() else None
        default_out = pa.get_default_output_device_info().get("index") if pa.get_device_count() else None
    except OSError:
        default_in = default_out = None
    lines = []
    try:
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            kinds = [k for k, n in (("in", info["maxInputChannels"]), ("out", info["maxOutputChannels"])) if n]
            marks = [m for m, d in (("default in", default_in), ("default out", default_out)) if d == i]
            api = pa.get_host_api_info_by_index(info["hostApi"])["name"]
            note = f"  ({', '.join(marks)})" if marks else ""
            lines.append(f"{i:>3}  {'/'.join(kinds):<6} {info['name']}  [{api}]{note}")
    finally:
        pa.terminate()
    return lines
