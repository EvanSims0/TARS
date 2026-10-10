# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

TARS is a push-to-talk voice assistant for one person's Windows PC: Deepgram Flux (speech to text)
→ a Claude tool loop → ElevenLabs (speech), acting on Gmail, Google Calendar, Todoist and
Open-Meteo. The spec is `docs/PRD.md`; the phase-gate acceptance test is `docs/TEST_SCRIPT.md`
(25 spoken phrases, including three safety checks). The project is in Phase 1 (Prototype).

## Commands

```bash
uv sync --locked --extra voice --extra desktop --extra dev   # Linux needs portaudio19-dev for PyAudio
uv run pytest -q                             # all offline tests (~3 s, no API calls, no spend)
uv run pytest tests/test_gate.py::test_clear_yes -q   # one test
uv run ruff check src tests                  # lint (rule set pinned in pyproject.toml)
uv run tars chat                             # same brain and tools as voice, by typing
uv run tars check                            # read-only live call to every account (needs real keys)
uv run tars restore                          # list backups; `tars restore latest` puts one back
```

After changing dependencies, run `uv lock` and commit `uv.lock`; CI installs with `--locked` and
runs on Windows and Linux. CI also checks that the base install (no `voice` extra) imports
`tars.app`, so don't import pipecat or pyaudio from modules outside `tars.voice` and `tars.check.audio_devices`.

## Architecture

- **`app.build_app`** wires everything: config, secrets, the tool registry, integrations (each only
  registered if its key or token exists), the gate and the agent. `tars chat`, `tars voice` and
  `tars status` all go through it.
- **`agent.Agent`** is a hand-written streaming tool loop (not the SDK tool runner). Haiku 5.5 runs
  with thinking off at low effort; the model calls the `think_harder` tool to switch the rest of the
  turn to Sonnet 5.5 (adaptive thinking, server-side refusal fallback). Near the spend cap it refuses
  to escalate; at the cap it makes no calls. Tool inputs are validated locally
  (`validate_input`) because eager input streaming skips server validation; a `max_tokens` stop never
  runs a tool. On interruption `_repair_history` keeps the message history valid.
- **Prompt caching depends on byte-stable prefixes**: `persona.SYSTEM_PROMPT` and the config-derived
  instructions are static, tools are sorted by name, and anything volatile (time, UTC offset,
  personality levels, mood) goes in the per-turn context line inside the user message. Keep it
  that way. History is append-only; thinking blocks are only stripped when sent to a different model.
- **`gate.ConfirmationGate` enforces safety in code, not in the prompt.** Each `actions.Tool` has a
  `Tier`: READ runs; CREATE_FOR_YOU runs and pushes an undo; AFFECTS_OTHERS is held, read back from
  the real inputs (`read_back`, which may fetch from the service) and runs only on a clean yes at the
  PC (`classify_reply`; hedged replies pass through to the model as a new request). From the phone it
  is parked as a draft via `park`. IRREVERSIBLE tools cannot be constructed. Low trust
  (`personality.confirm_own_actions`) holds CREATE_FOR_YOU actions on accounts too. A held action
  expires after 120 s.
- **Untrusted content**: email (and calendar listings) pass through `sanitize` (hidden HTML and
  invisible characters stripped) and `wrap_untrusted`. Results with `untrusted=True` taint the
  conversation, which adds a warning to later read-backs.
- **Network**: every integration shares `net.client()`, which retries a GET once after a dropped
  connection or a 502/503/504, and a POST/PATCH/DELETE only when it never left the PC
  (`extensions={"idempotent": True}` opts in a POST that changes nothing). Timeouts aren't retried.
  The retry lives in `Client.send`, not a custom transport, because a transport drops env proxies.
  `GoogleSession.request` renews the token and repeats once on a 401.
- **Integrations** (`integrations/`) are thin async httpx clients that raise `actions.ToolError` with
  a one-sentence, speakable message; the agent turns those into `is_error` tool results. Their
  vendor field names were written without live docs, so `tars check` and the Gate 1 script are the
  real verification.
- **Voice** (`voice/`): `TarsBrain` is the Pipecat processor between STT and TTS; `PhraseChunker`
  feeds speakable phrases to TTS early; `PushToTalkInput` opens the mic only on the hotkey and
  closes it after `follow_up_seconds` so a Bluetooth headset stays in high-quality mode.
- **Desktop UI** (`ui/`): the design system is `ui/static/tars.css` + `tars.js` (tokens from the
  "BRICK – AI PA concept" Figma file: surfaces `#36454f`/`#2b3840`/`#405260`/`#4b5e6c`, text white and
  `#d3d3d3`, slate `#708090` for borders only; system fonts; every state a distinct shape; motion off
  under reduced-motion). Pages live in `ui/pages/`. `ui/server.py` (`AppServer`) is a stdlib HTTP
  server on 127.0.0.1 with a random port; every page and API call needs the random token and a
  loopback Host header, and POSTs must be JSON. Account actions run on TARS's own loop via
  `ctx.loop`; overlay buttons become typed turns via `ctx.submit`; tray actions use `ctx.controls`.
  `ui/live.py` (`LiveState`) is what the overlay long-polls (`/api/live?since=version`); the agent,
  brain and mic write to it. `ActionLog` records every non-read action with its undo id for History.
  Settings write through `config_edit.set_values` (keeps comments; only keys in `EDITABLE`).
  `ui/desktop.py` is the Windows shell (pystray tray, pywebview overlay and windows), a separate
  process that only talks to the server; its token travels in the environment, not argv.
- **Cue light**: the personality guide asks the model to start a joking sentence with `⁂`
  (`JOKE_MARK`); `Agent.handle`'s `say` strips it before speech and transcripts and lights the cue.
- **State on disk** lives under `%APPDATA%\TARS` (or `TARS_HOME`; `~/.tars` elsewhere): config.toml,
  the Obsidian memory vault, transcripts and logs (kept `privacy.transcript_days`), spend and
  per-turn timing logs. Keys live in the OS credential store via `secrets` (env vars override).
  `logs.start` gives every command (and the desktop shell) its own daily log with crashes in it
  (`diagnose=False`, so tracebacks never show variable values). `backup.py` copies the vault,
  config and data folder (not transcripts or logs) into a dated folder, verified before it's
  renamed into place; `restore` keeps the previous state as `before-restore`.
- **Speech text**: `sanitize.for_speech` drops emoji before TTS (`TarsBrain.speak` and `announce`);
  screens keep them. UI text that can be long (addresses, names) must wrap: cards and wells use
  `overflow-wrap: anywhere`, and grid/flex children need `minmax(0, 1fr)` or `min-width: 0`.

## Tests

Tests never touch the network. `tests/test_ui.py` covers the app server's security and APIs with a
real server on a random port. Agent tests script model responses with `conftest.FakeBackend` and
`reply/text/tool_use/thinking`; integration tests mock HTTP with `respx`; `test_backend.py` runs the
real Anthropic SDK against a mocked SSE stream via `httpx2`. Use the `make_agent` fixture, which
pins the clock and the `America/New_York` timezone.

## Product rules that constrain code

- Nothing that deletes data, buys or pays may be added (PRD non-goal for v1).
- Anything that reaches other people must go through the gate with a read-back built from real
  data, never from model-supplied descriptions.
- Spoken output is plain text: no markdown, short, lists capped at three. Humor never appears in
  confirmations, errors, security, money or health (see `personality.PERSONALITY_GUIDE`).
- The character and voice are original; don't imitate the film character.
