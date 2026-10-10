# Setting up TARS on your PC

About an hour, once. Everything here is on Windows 11 or 10. `uv` installs Python 3.12, the version
TARS is tested on (`.python-version` pins it), even if a newer Python is already on the PC.

## 1. Install

The versions that passed the tests are pinned in `uv.lock`; `uv` installs exactly those.

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"   # once; then open a new window
git clone -b main https://github.com/EvanSims0/TARS
cd TARS
uv sync --locked --extra voice --extra desktop
.venv\Scripts\activate
tars setup
```

`tars setup` opens the setup wizard: your name and home, each key with a live check, Google,
your headset, start at login, and a first-run check of everything. The sections below are the
same steps by hand, if you'd rather.

Without `uv`, `py -m venv .venv`, `.venv\Scripts\activate` and `pip install -e ".[voice]"` also work,
but install the newest versions rather than the tested ones.

`tars init` (or the wizard) writes `%APPDATA%\TARS\config.toml`. Open it and fill in your name, your Gmail
address (`user_email`, so "email me" works), home address, latitude, longitude and timezone.

## 2. Accounts and keys

Each key goes into Windows Credential Manager with `tars set-key NAME`, which prompts for
the value without echoing it. Keys never go in `config.toml` or the repo.

| Service | Where | Key name | Notes |
|---|---|---|---|
| Anthropic API | console.anthropic.com → API keys | `ANTHROPIC_API_KEY` | Set a monthly limit in the console too, as a backstop to TARS's own $25 cap |
| Deepgram | console.deepgram.com → API keys | `DEEPGRAM_API_KEY` | TARS opts each session out of the model-improvement program (`mip_opt_out`) |
| ElevenLabs | elevenlabs.io → Profile → API keys | `ELEVENLABS_API_KEY` | Starter plan. Pick or design an **original** voice (not a clone of an actor), and put its voice ID in `voice.tts_voice_id` |
| Todoist | todoist.com → Settings → Integrations → Developer | `TODOIST_API_TOKEN` | Create a project named `Shopping` or let TARS create it |
| Google Maps (optional) | console.cloud.google.com → enable *Routes API* → Credentials → API key | `GOOGLE_MAPS_API_KEY` | Needs a billing account. Without it, leave-by times are off (test 3 fails) and everything else works. Restrict the key to the Routes API |
| Google (Gmail, Calendar) | see below | (stored by `tars google-auth`) | |

Turn off optional data use where offered (Deepgram model improvement, ElevenLabs history
retention if you like).

### Google OAuth client

1. In Google Cloud console, create a project, then enable the **Gmail API** and the **Google Calendar API**.
2. *OAuth consent screen*: user type External, add yourself as a test user, add the scopes
   `gmail.modify`, `gmail.compose`, `calendar.events` and `calendar.readonly`.
3. **Publish the app** (Publishing status → In production). Leave it unverified; that is fine
   for personal use and stops the sign-in expiring every 7 days.
4. *Credentials* → Create OAuth client ID → Desktop app → download the JSON.
5. Run `tars google-auth path\to\client_secret.json` and approve in the browser. Google warns
   that the app is unverified; continue, since it's your own.

## 3. Try it

```powershell
tars check           # one read-only call to every account; fix anything marked FAIL
tars devices         # list audio devices, if the headset isn't the Windows default
tars probe -n 10     # Phase 1 test: Claude Haiku 5.5's real time to first word
tars chat            # talk by typing, same brain and tools as voice
tars voice           # push-to-talk: Ctrl+Alt+Space to talk, Ctrl+Alt+M to mute
tars status          # what's connected, today's response times and spend
tars memory          # the memory map: browse, search and forget what TARS remembers
tars ui history      # the History window (also settings, status, overlay)
```

With `tars voice` running on Windows, TARS sits in the tray: right-click it for History, Settings,
Memory and Status. The overlay appears bottom-right while you talk, never takes focus from the app
you're in, and can be dragged; it remembers where you put it.

`tars check` creates, changes and sends nothing. Its Claude call costs a fraction of a cent. If
the headset isn't the Windows default device, put the numbers `tars devices` shows into
`voice.input_device_index` and `voice.output_device_index` in `config.toml`.

The memory map also opens when you say "show me my memory". It runs on this PC only, at an
address with a one-time key in it, and updates as TARS learns new things. Click a branch to zoom
in, a fact to see when it was saved, or a name at the top to see every fact that mentions it.

With `tars voice`, press the talk hotkey and speak. The mic stays open for about 8 seconds
after TARS answers, so follow-ups don't need the hotkey, then closes so the Bluetooth
headset goes back to high-quality audio. Press the hotkey or start talking while TARS
speaks to interrupt it; "stop" or "cancel" always works.

## 4. Start at login and back up nightly

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows-tasks.ps1
```

This registers two Task Scheduler tasks for your user:

- **TARS** runs `tars voice` at logon (a logon task rather than a service, so it can reach your audio).
- **TARS vault backup** runs `tars backup` nightly at 3:00, waking the PC if needed, and keeps 30 days of copies.

Each backup holds the memory vault, `config.toml` and the data folder (personality, spend, the
action log, parked drafts), and is checked file by file against the originals before it counts.
Conversations and logs are left out on purpose: they're kept 7 days and "forget" has to mean it.
Keys stay in Windows Credential Manager and are never copied.

To put a backup back, quit TARS (tray menu, Quit), then:

```powershell
uv run tars restore            # lists the backups, newest first
uv run tars restore latest     # or a date, e.g. tars restore 2026-10-09
```

What you had just before the restore is kept as `before-restore`, so `tars restore before-restore`
undoes it.

## Where things live

| What | Where |
|---|---|
| Config | `%APPDATA%\TARS\config.toml` |
| Memory vault (open it in Obsidian) | `%APPDATA%\TARS\vault` unless `vault_path` is set |
| Backups (vault, settings, history) | `%APPDATA%\TARS\vault-backups` unless `backup_path` is set |
| Transcripts (text only, 7 days) | `%APPDATA%\TARS\data\transcripts` |
| Daily logs, crashes included, one per command (7 days) | `%APPDATA%\TARS\data\logs` (`tars-voice-*.log`, `desktop-*.log`, …) |
| Per-turn timing and spend | `%APPDATA%\TARS\data\turns-*.jsonl`, `spend-*.jsonl` |
| API keys and tokens | Windows Credential Manager, under "TARS" |
