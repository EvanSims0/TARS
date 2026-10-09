# TARS

A hands-free everyday voice assistant powered by Claude. It runs on your Windows PC: press a
hotkey and talk through your headset, and it handles email, calendar, reminders and lists
through your own accounts. It reads back anything that affects other people and waits for a yes.

The product spec is in [docs/PRD.md](docs/PRD.md), setup is in [docs/SETUP.md](docs/SETUP.md), and the
phase-gate test script is in [docs/TEST_SCRIPT.md](docs/TEST_SCRIPT.md).

## How it fits together

```
push-to-talk mic ─► Deepgram Flux ─► TarsBrain ─► ElevenLabs Flash v2.5 ─► headset
 (open only while      (end of turn)    │  ▲          (speaks phrase by phrase)
  you're talking)                       ▼  │
                                Agent (Claude API streaming loop)
                         Haiku 5.5 by default · Sonnet 5.5 via think_harder
                                        │
                              Confirmation gate (code)
                                        │
          Gmail · Google Calendar · Todoist · Google Routes · Open-Meteo · timers · memory vault
```

| Module | What it does |
|---|---|
| `src/tars/agent.py` | The streaming tool loop: model choice and escalation, spoken cues, tool validation, spend and timing per turn |
| `src/tars/gate.py` | The confirmation gate. Actions that affect others are held, read back from their real inputs, and run only on a clean "yes" at the PC; from the phone they become drafts |
| `src/tars/actions.py` | Tools and their tiers (read / create for you / affects others); irreversible tools can't be registered |
| `src/tars/integrations/` | Gmail, Google Calendar, Todoist (store-sectioned shopping list), Google Routes, Open-Meteo |
| `src/tars/sanitize.py` | Strips HTML, hidden text and invisible characters from email and fences it as untrusted data |
| `src/tars/memory.py` | The Obsidian vault: facts appended under known headings, "forget that", nightly backup |
| `src/tars/spend.py` | Cost ledger for Claude, Deepgram and ElevenLabs; stops escalating near the cap and pauses at it |
| `src/tars/voice/` | Pipecat pipeline, push-to-talk mic with an 8-second follow-up window, phrase chunker, interruption |

## Status: Phase 1 (Prototype)

| Built and tested | Next |
|---|---|
| Push-to-talk voice loop on Pipecat (Deepgram Flux → Claude → ElevenLabs), interruptible, "stop"/"cancel" | Run the four Phase 1 tests on the real PC and headset (`tars probe`, turn detection, interruption) |
| Calendar: read, find free time, add/move your own events, invites through the gate | Telegram phone remote and the Cloudflare offline relay |
| Todoist reminders, tasks and the shopping list by store, with undo | "Hey TARS" wake word (livekit-wakeword) |
| Email: summarise unread, read, archive/label, drafts; sending gated with a full read-back | Tray icon, overlay, history and settings windows |
| Weather and traffic-aware leave-by times, named timers | Recorded "I'm offline" message |
| Memory vault with "noted" / "forget that", 7-day transcripts, nightly backup | Proactive features (Phase 3) |
| Morning brief at 9am, or as soon as the PC is on after 9 | |
| Spend cap, per-turn response time and cost log, `tars status` | |
| `tars check` (read-only test of every account), `tars devices`, daily log file | |
| CI on Windows and Linux, locked dependencies | |
| Personality sliders (humor, bluntness, trust), Vela calm mode, discretion and stress dial-down, meeting tally | |

### Passing Gate 1

The code is done and tested offline; what's left happens on the PC.

1. Create the Anthropic key (with a monthly limit in the console) and the Todoist account and token.
2. Install and fill in `config.toml` ([docs/SETUP.md](docs/SETUP.md)), store the keys with
   `tars set-key`, and run `tars google-auth`.
3. Run `tars check` until nothing fails, then `tars probe -n 10`.
4. Run the 25 phrases in [docs/TEST_SCRIPT.md](docs/TEST_SCRIPT.md). Gate 1 opens when at least 23
   pass first time, all three safety checks pass, and 9 in 10 replies start within 3 seconds.

## Personality

Say any of these and TARS changes on the spot (and remembers):

| Say | What happens |
|---|---|
| "Humor 40%" | Fewer jokes. 0% is none; jokes never make a reply longer |
| "Bluntness 90%" | Less tact about your drafts and plans. Facts stay exact at every level |
| "Trust 60%" | Below 80%, TARS asks before changing your lists, calendar or email filing. Raising trust needs your yes |
| "Vela mode" / "back to TARS" | Vela is the calm, joke-free assistant; it can have its own voice (`calm_voice_id`) |

Humor drops to 0 while a Zoom, Teams or Webex meeting is running or a calendar event looks like a
presentation, and to 30% when you have back-to-back meetings or tasks still open after 9pm.
The morning brief reads like a pre-launch checklist, and "How much time did I spend in meetings?"
gives the time-dilation report.

## Development

```bash
uv sync --locked --extra voice --extra dev   # Linux needs portaudio19-dev for the mic; Windows bundles it
uv run pytest -q
uv run ruff check src tests
uv run tars chat                             # the same brain and tools, by typing
```

After changing dependencies, run `uv lock` and commit `uv.lock`. CI runs lint and tests on
Windows and Linux for every push.

Tests use a scripted model backend and mocked HTTP, so they make no API calls and cost nothing.
