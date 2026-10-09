# Setting up TARS on your PC

About an hour, once. Everything here is on Windows 11 or 10 with Python 3.11 or newer.

## 1. Install

```powershell
git clone https://github.com/EvanSims0/TARS
cd TARS
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[voice,dev]"
tars init
```

`tars init` writes `%APPDATA%\TARS\config.toml`. Open it and fill in your name, your Gmail
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
| Google Maps | console.cloud.google.com → enable *Routes API* → Credentials → API key | `GOOGLE_MAPS_API_KEY` | Restrict the key to the Routes API |
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
tars status          # what's connected
tars probe -n 10     # Phase 1 test: Claude Haiku 5.5's real time to first word
tars chat            # talk by typing, same brain and tools as voice
tars voice           # push-to-talk: Ctrl+Alt+Space to talk, Ctrl+Alt+M to mute
```

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

## Where things live

| What | Where |
|---|---|
| Config | `%APPDATA%\TARS\config.toml` |
| Memory vault (open it in Obsidian) | `%APPDATA%\TARS\vault` unless `vault_path` is set |
| Vault backups | `%APPDATA%\TARS\vault-backups` unless `backup_path` is set |
| Transcripts (text only, 7 days) | `%APPDATA%\TARS\data\transcripts` |
| Per-turn timing and spend | `%APPDATA%\TARS\data\turns-*.jsonl`, `spend-*.jsonl` |
| API keys and tokens | Windows Credential Manager, under "TARS" |
