# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

TARS is a push-to-talk voice assistant for one person's Windows PC: Deepgram Flux (speech to text)
→ a Claude tool loop → ElevenLabs (speech), acting on Gmail, Google Calendar, Todoist and
Open-Meteo. The spec is `docs/PRD.md`; the phase-gate acceptance test is `docs/TEST_SCRIPT.md`
(25 spoken phrases, including three safety checks). The project is in Phase 1 (Prototype).

## Commands

```bash
uv sync --locked --extra voice --extra dev   # Linux needs portaudio19-dev for PyAudio
uv run pytest -q                             # all offline tests (~3 s, no API calls, no spend)
uv run pytest tests/test_gate.py::test_clear_yes -q   # one test
uv run ruff check src tests                  # lint (rule set pinned in pyproject.toml)
uv run tars chat                             # same brain and tools as voice, by typing
uv run tars check                            # read-only live call to every account (needs real keys)
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
- **Integrations** (`integrations/`) are thin async httpx clients that raise `actions.ToolError` with
  a one-sentence, speakable message; the agent turns those into `is_error` tool results. Their
  vendor field names were written without live docs, so `tars check` and the Gate 1 script are the
  real verification.
- **Voice** (`voice/`): `TarsBrain` is the Pipecat processor between STT and TTS; `PhraseChunker`
  feeds speakable phrases to TTS early; `PushToTalkInput` opens the mic only on the hotkey and
  closes it after `follow_up_seconds` so a Bluetooth headset stays in high-quality mode.
- **Memory map** (`memory_map.py` + `memory_map.html`): a stdlib HTTP server on 127.0.0.1 (random
  port, started on first use) serving one self-contained page and a JSON API. Every request needs
  the random token and a loopback Host header. The page polls `/api/memory` and can only forget
  facts (`Vault.remove_line`), never add or edit them. `Vault` holds a lock because this server
  thread writes to it too.
- **State on disk** lives under `%APPDATA%\TARS` (or `TARS_HOME`; `~/.tars` elsewhere): config.toml,
  the Obsidian memory vault, transcripts and logs (kept `privacy.transcript_days`), spend and
  per-turn timing logs. Keys live in the OS credential store via `secrets` (env vars override).

## Tests

Tests never touch the network. Agent tests script model responses with `conftest.FakeBackend` and
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
