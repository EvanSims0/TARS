# Everyday Voice Assistant PRD

Oct 7, 2026 · @Evan

## Overview

A hands-free personal assistant, powered by Claude, that runs everyday life by voice: errands, bills, health routines, people, travel and daily briefings. It runs on your PC: you talk to it there or message it from your phone, it does the work on the PC through your connected accounts, and it confirms out loud before doing anything that affects other people.

**The problem.** Everyday admin is scattered across email, calendar, lists and apps, and much of it comes up when your hands are busy: cooking, driving, getting ready. Smart speakers handle timers and weather but can't reason across your accounts. Chat assistants can, but need a screen and need you to start the conversation.

**The bet.** Joining the two gives an assistant you talk to like a person, that knows your schedule and commitments, and that speaks up when something needs you.

## Decisions

Version 1 runs entirely on your PC, with your phone as a remote; the stack below was checked against vendor docs on 2026-10-07 and should cost about $8–40 a month.

| Decision | Choice | Why |
|---|---|---|
| Name and wake phrase | TARS; push-to-talk by default, with an optional "Hey TARS" wake word | Your pick; a two-part phrase triggers by accident far less than a single short word. Custom wake phrase trained with livekit-wakeword |
| Personality | Dry wit, one-sentence replies by default, tuned by sliders: humor (default 70%), bluntness (tact only, never accuracy) and trust (below 80%, it asks before changing your accounts); a calm, joke-free mode called Vela | Helpful first, with the occasional deadpan line; say "more" for detail. Humor drops to 0 while you present and to 30% when you're swamped, and never appears in confirmations, errors, security, money or health matters. An original personality with original lines, not an imitation of the film character |
| First jobs | Email triage, calendar, reminders and lists | Your top three; these lead Phase 1 |
| Where it runs | Your Windows PC, started at login by Task Scheduler | Push-to-talk hotkey through your Bluetooth headset, so its mic isn't held open in low-quality call mode all day; Windows wakes the PC for scheduled briefs and alerts; a logon task rather than a Windows service, so it can reach your audio |
| Build approach | Standalone Python app on Pipecat, built and tested by Claude on your PC; you try each phase and give feedback | Runs the voice pipeline locally with no server; LiveKit Agents needs one outside development (Pipecat) |
| Phone | Remote only, through a private Telegram bot on your iPhone, with an offline relay | Messages go to a small free Cloudflare Worker that replies "queued" when the PC is asleep and hands them over when it wakes, so no ports open on the PC; bot chats are not end-to-end encrypted (Telegram Bot API) |
| Email and calendar | Gmail and Google Calendar, called directly | Your own Google OAuth client, published so sign-in doesn't expire every 7 days (Google Cloud Help) |
| Reminders and lists | Todoist, API v1 | Due-time reminders are free; custom reminder times need Pro (Todoist) |
| Wake word engine | livekit-wakeword, openWakeWord as fallback | openWakeWord's last release was February 2024; Porcupine's free tier ended June 2026 (livekit-wakeword) |
| Speech to text | Deepgram Flux | Detects end of turn itself (about 260 ms, vendor claim); $200 signup credit (Deepgram) |
| Text to speech | ElevenLabs Flash v2.5 | About 75 ms model latency; Starter plan $6 a month covers about 3 hours of speech (ElevenLabs) |
| Brain | Own streaming loop on the Claude API: Haiku 5.5 by default, Sonnet 5.5 for multi-step tasks | Haiku 5.5 can turn thinking off and costs $0.10 / $0.50 per million tokens; the Agent SDK adds a subprocess and suits background jobs better (Claude pricing) |
| Memory | A new Obsidian vault just for TARS, on the PC with no sync, edited as plain files; saves automatically and says "noted" | Small appends under known headings; one sync service only (Obsidian Sync) |
| Travel time and weather | Google Routes (traffic-aware), Open-Meteo | Routes gives 5,000 free traffic requests a month; Open-Meteo is free with no key |
| Expected cost | About $8–40 a month, capped at $25 | Mostly the $6 voice plan and how often it escalates to Sonnet; near the cap it stops escalating and tells you |

## Goals and non-goals

Version 1 succeeds if it becomes the default way you handle daily admin, used every day without frustration.

**Goals**

- Handle capture, questions and simple actions by voice, with the first spoken word back within 1.5 seconds for simple requests.
- Be proactive: raise what matters (bills due, time to leave, birthdays) without being asked, a few times a day at most.
- Never act on someone else's behalf (send, invite, reply) without a spoken read-back and a clear yes.
- Share one memory across voice and chat, so things you say once carry over.

**Non-goals for v1**

- Hobby- or work-specific workflows.
- Purchases, payments or moving money.
- Multiple household members with separate private data (single owner first).
- Replacing a music or media speaker.

## Target user and use cases

The primary user is one person managing their own day, at home and on the go. These moments define what v1 must handle well:

| Moment | Where | What you say | What happens |
|---|---|---|---|
| Getting ready | PC | "Good morning." | Spoken brief: today's events, weather, bills due, anything urgent in email |
| At your desk | PC | "Add eggs and coffee to the list." | Added to Todoist under the right store |
| Before heading out | PC | "When do I need to leave for the dentist?" | Leave-by time based on traffic, with a reminder set |
| On the go | Phone voice note | "Anything urgent in my email?" | Voice reply with the top messages; offers to draft replies |
| Anywhere | PC or phone | "Remind me to call Mom on Sunday." | Todoist reminder created and confirmed in one line |
| Travel day | Phone | "What's my hotel confirmation number?" | Read from the booking email and sent as text |
| Unprompted | Phone or PC | (assistant) "Sam's birthday is Friday. Want gift ideas?" | Proactive nudge, a few days ahead |
| Winding down | PC | "What's left for tomorrow?" | Summary of open tasks and the first event tomorrow |

## Features

P0 is the minimum to use it daily; P1 makes it proactive; P2 extends where and how you reach it.

### P0: MVP

| Feature | What it does |
|---|---|
| Wake word and push-to-talk | Push-to-talk hotkey by default; "Hey TARS" wake word can be switched on |
| Natural conversation | Follow-ups for about 8 seconds without repeating the wake word; you can interrupt it mid-sentence |
| Phone remote | Voice notes or text to a private Telegram bot; replies as voice and text; anything involving other people waits as a draft until you're at the PC |
| Offline relay | When the PC is asleep, the bot replies right away that your request is queued, and the PC picks it up on waking |
| Reminders and to-dos | Create, list and complete by voice in Todoist, with due-time reminders that reach your phone |
| Shopping lists | One running Todoist list, sectioned by store, read back on request |
| Calendar | Read your schedule, add or move your own events, find free time |
| Email triage | Summarize unread mail, flag what needs a reply, draft replies, archive or label on its own; sending needs your confirmation at the PC |
| Morning brief | Spoken summary of the day, on request or on a schedule |
| Quick answers | Weather, timers, conversions and general questions |
| Memory | Saves facts it picks up to the Obsidian vault and says "noted"; "forget that" removes them |
| Confirmation gate | Enforced in code, not left to the model: reads back anything that affects others and waits for a yes |
| Status and spend | On screen and via /status in Telegram: what's connected, today's response times and spend |

### P1: Proactive help

| Feature | What it does |
|---|---|
| Bills and subscriptions | Finds due dates and renewals in email; reminds you 3 days before |
| People | Birthday and anniversary heads-ups, nudges about messages left unanswered, gift ideas |
| Travel | Pulls bookings from email, alerts you when to leave for the airport, reads check-in details |
| Leave-now alerts | Traffic-aware departure reminders for events with a location |
| Health routines | Medication and water reminders; keeps a list of questions for your next appointment |
| Evening wind-down | What got done, what's open, and tomorrow's first event |
| Cooking mode | Recipes one step at a time, several named timers, substitutions |

### P2: Expansion

| Feature | What it does |
|---|---|
| Smart home | Lights, thermostat and named routines by voice |
| More rooms | Small speakers around the house that send audio to the PC |
| Household members | Recognizes who is speaking and keeps each person's data private |
| Phone call-in | Call a number to talk to it, as an alternative to Telegram |
| Learning mode | Quizzes, language practice, articles explained aloud |
| Wearables | Trigger from earbuds or a watch |

## Voice experience

It should feel like talking to a capable person in the room: quick, brief, and easy to interrupt.

| Principle | Requirement |
|---|---|
| Fast | First spoken word within 1.5 s for simple requests and 3 s when it uses your accounts; if longer, a short cue such as "checking your calendar" |
| Brief | Answer first, detail only if asked; spoken replies under about 20 seconds |
| Interruptible | Talking over it stops playback within 0.3 s; "stop" and "cancel" always work |
| No long lists aloud | Read the top 3 items, then ask "want the rest?" |
| Show what's hard to hear | Addresses, numbers and links also go to your phone as a Telegram message |
| Polite when proactive | At most 3 unprompted alerts a day, none between 10pm and 8am |
| Plain failures | Say what went wrong in one sentence and offer the next step |

## Interface

The assistant is mostly heard, not seen: on the PC it lives in the system tray and shows a small overlay only while you're talking to it; on the phone it is a Telegram chat. The look is Modern Minimalist in dark mode, with expressive motion while it talks.

**On the PC**

| Surface | Purpose | Shows |
|---|---|---|
| Tray icon | Always-on presence | Current state; menu for mute, history, settings and quit |
| Overlay | Appears during a conversation, fades after | Live transcript, the reply, cards for addresses or lists, Yes / No for confirmations |
| History window | Review and undo | Past conversations, actions taken with undo, today's response times and spend |
| Settings window | Tune behavior | Voice, wake word, quiet hours, alert rules, connected accounts, spend cap |

**States.** Each state has its own icon shape and an optional sound, so none relies on color alone.

| State | Meaning |
|---|---|
| Idle | Waiting for the wake word or hotkey |
| Listening | Hearing you; transcript appears live |
| Thinking | Working on it; says a short cue if it takes longer than about 1.5 s |
| Speaking | Talking; you can interrupt |
| Needs confirmation | Waiting for your yes or no |
| Muted | Microphone off |
| Problem | A service or account is down; says which one |

**On the phone (Telegram)**

- Replies come back as a voice bubble with the same text underneath.
- Anything involving other people (emails, replies, calendar invites) is saved as a draft until you're back at the PC, and the bot tells you what's waiting.
- Proactive alerts are short text messages, grouped when several arrive together.
- Commands: /status, /mute, /brief, /undo.

**Accessibility.** Every action has a keyboard shortcut, text follows the system size setting, animation respects reduced-motion settings, and text contrast is at least 4.5:1.

**Visual direction.** Grayscale and quiet, so the only thing that moves is TARS itself. Slate is too low-contrast on charcoal for text, so it is used only for borders and the idle visual.

| Token | Color | Used for | Contrast on background |
|---|---|---|---|
| Background | Charcoal `#36454f` | Overlay and window surfaces | n/a |
| Text | White `#ffffff` | Transcript, replies, labels | About 10:1 |
| Secondary text | Light gray `#d3d3d3` | Timestamps, hints, captions | About 6.6:1 |
| Accent | Slate gray `#708090` | Borders, dividers, idle visual | About 2.4:1, so not used for text |

Font: DejaVu Sans, or the Windows system font if you prefer it native. Motion: while TARS speaks, a visual reacts to its voice; listening and thinking get their own distinct movement. With Windows' reduce-animations setting on, it falls back to simple state changes.

## How it works

Every action passes a confirmation gate before it reaches your accounts.

```
Your devices:  Your PC (push-to-talk via headset) · Phone via Telegram (voice notes, replies, alerts) · Offline relay (queues requests while PC sleeps)
        │                                                     ▲
        ▼                                                     │
Speech to text (Deepgram Flux, detects turn end)     Text to speech (ElevenLabs, speaks as it streams)
        │                                                     ▲
        ▼                                                     │
Memory (Obsidian vault) ◄──► Claude agent (understands the request, plans, calls tools;
                              Haiku 5.5 by default, Sonnet 5.5 for hard tasks) ◄── Scheduler (briefs and alerts)
                                         │
                                         ▼
                     Confirmation gate (code-enforced read-back before anything is sent)
                                         │
                                         ▼
Your accounts and services: Email (read, draft, send) · Calendar (events, free time) · To-dos and lists (Todoist) · Maps and weather
```

Your words become text, the Claude agent decides what to do using memory and your accounts, and the reply streams back as speech. The scheduler lets the assistant start a conversation itself for briefs and alerts.

**Response-time budget.** Every stage streams so they overlap; the estimate for this stack is 1.0–1.6 s from the end of your sentence to its first word, set mostly by how fast Claude starts replying.

| Stage | Estimate | Basis |
|---|---|---|
| Deepgram Flux detects you've finished | About 260 ms | Vendor claim |
| Claude's first words, network included | About 400–700 ms | Estimated; Haiku 5.5 is not yet measured |
| Waiting for the first speakable phrase | About 100–300 ms | Text goes to the voice after about 8–12 words |
| ElevenLabs first audio, network included | About 150–250 ms | Vendor 75 ms plus network |
| Playback buffer | About 50 ms | |

## Privacy and safety

The assistant listens in your home and acts in your accounts, so trust is a feature, not an add-on.

**Action tiers**

| Tier | Examples | Rule |
|---|---|---|
| Read | Check calendar, summarize email, weather | No confirmation needed |
| Create for you | Reminders, list items, your own calendar holds, archiving or labeling email | Do it, say what was done in one line; "undo" works |
| Affects others | Send an email or message, send an invite, reply | Read back in full, wait for an explicit yes |
| Irreversible or money | Deleting data, purchases, payments | Not supported in v1 |

**Rules**

- Wake word detection runs on the PC; audio leaves it only after you trigger it.
- No raw audio is stored; transcripts stay on the PC for 7 days and "forget that" deletes them.
- A mute hotkey and an on-screen indicator whenever it's listening.
- Email and web content reaches the model only as tool results, with HTML and hidden text stripped, and is treated as information, never as instructions.
- Once a conversation has read email, anything that sends, invites or shares needs confirmation, and nothing an email asks for is done automatically.
- The confirmation gate is enforced in code, so the model cannot skip it.
- Telegram is not end-to-end encrypted: codes, passwords and account numbers are shown on the PC screen only, never sent through the bot.
- The bot ignores every account but yours, checked before any audio is downloaded.
- The bot token and API keys live in the operating system's credential store.
- Google access is limited to reading mail, drafting and sending, and calendar events; no full mailbox access.

## Success metrics

These are starting targets for personal use, to revisit after the first month.

| Metric | Target |
|---|---|
| End of speech to first spoken word | 1.5 s typical, under 3 s for 9 in 10 requests |
| False wakes | Fewer than 1 per day |
| Requests done right the first time | 90% or more |
| Wrong messages sent | Zero |
| Daily use after 4 weeks | 5 or more interactions a day |
| Proactive alerts you find useful | At least half; the rest tune the alert rules |
| Transcription accuracy in your home | Fewer than 8 words wrong per 100 |
| Monthly running cost | Under the $25 cap |

The app logs response time and cost for every turn, and a fixed script of 25 real phrases is re-run at each phase gate.

## Roadmap

Daily use at home arrives by week 6, proactive help follows.

| Phase | Weeks | Scope | Gate to pass before the next phase |
|---|---|---|---|
| Prototype | 1 to 2 | Audio and latency tests, push-to-talk loop, calendar and reminders, email summaries and drafts | Gate 1: end to end, under 3 s |
| Daily-use MVP | 3 to 6 | Telegram phone remote, wake word, email sends, lists, morning brief, memory, read-back | Gate 2: used daily for 2 weeks |
| Proactive | 7 to 10 | Bills, people, travel, leave-now alerts, health, wind-down, cooking mode | Gate 3: half of alerts useful |
| Expand | 11 onward | Phone call-in, smart home, more rooms, household members | |

Each phase starts only once the gate before it is met. Phase 1 opens with four tests: Haiku 5.5's real response time, Deepgram's turn detection inside Pipecat, wake word accuracy in your room, and interrupting it through your speakers. Durations assume part-time building.

## Risks and open questions

| Risk | Mitigation |
|---|---|
| Claude's response speed is unmeasured: Haiku 5.5 launched 2026-10-07 | Measure in Phase 1; the model is a config setting, so it can be swapped |
| It hears itself through speakers and cuts itself off | You chose a headset, which avoids this; re-test if you ever switch to speakers |
| Emails try to steer the assistant | Content passed only as data; confirmation required after reading email; gate enforced in code |
| Google sign-in expires every 7 days | Publish the OAuth app to production as unverified personal use |
| Some prices are promotions (Deepgram Flux rate, ElevenLabs v4 offer) | Speech providers are swappable; spend tracked from week 1 |
| openWakeWord is no longer maintained | Use livekit-wakeword; compare both on your own phrase |
| The PC is asleep when you message from your phone | The offline relay replies right away and queues the request |
| Alerts become noise | Daily cap, quiet hours, and "don't tell me about this again" |

**Gaps found in the final review**

| Gap | Fix |
|---|---|
| Scheduled briefs and alerts can't run while the PC sleeps | Windows wakes the PC for each scheduled job, then lets it sleep again; Todoist reminders reach your phone regardless |
| A Bluetooth headset drops to low-quality call audio while its mic is open | Push-to-talk by default; the "Hey TARS" wake word can be switched on, or added later with a desk mic |
| No internet means no speech or replies | A recorded "I'm offline" message plays locally; Todoist reminders keep working |
| The memory vault has no backup | Nightly backup copy to a second location, kept 30 days |
| The offline relay stores your messages until the PC collects them | Encrypted with a key only the PC holds, deleted on pickup, and accepted only with Telegram's secret header |
| Your audio and email text go to Anthropic, Deepgram and ElevenLabs | Turn off any optional data use in each account, such as Deepgram's model-improvement program |
| The voice and look could drift into copying the film | An original ElevenLabs voice (cloning an actor's voice breaks its rules) and an original visual; rename it if you ever share or publish it |
| The $25 cap's scope was unclear | It covers everything billed by usage: Claude, Deepgram and ElevenLabs |

## Needs your input

Twenty-eight of 38 items are done (as of 2026-10-09) and nothing blocks Phase 1; the 10 left have sensible defaults, except account setup, which opens Phase 1. Answers move into Decisions as they come in.

**Your PC and phone**

- [x] 1. Which operating system does the PC run? **Windows.**
- [x] 2. What audio setup? **Headset.**
- [x] 3. What happens if the PC is asleep when you message it? **Add the offline relay.**
- [ ] 4. Which rooms are within earshot of the PC?
- [x] 5. Which phone, and is Telegram acceptable? **iPhone; Telegram is fine.**

**Who it is**

- [x] 6. What should it be called, and what wake phrase? **TARS, woken with "Hey TARS".**
- [x] 7. Personality? **Dry wit.**
- [x] 8. Voice: which of a few samples you like best, and how fast it should talk. **An original voice designed in ElevenLabs (voice ID in the config).**
- [ ] 9. How should it address you, and how much small talk is welcome?
- [x] 10. Default reply length? **One sentence.**

**Day to day**

- [x] 11. Which three jobs must it do well in week one? **Email triage, calendar, reminders and lists.**
- [x] 12. Morning brief: what time, started how (you say "good morning" or it starts itself), and what goes in it? **9am, or as soon as the PC is on after 9; once a day; events, weather, tasks due and urgent email.**
- [x] 13. Which alerts may interrupt you, how many, and quiet hours? **Up to 3 a day; quiet from 10pm to 8am.**
- [x] 14. What makes an email urgent: particular people, keywords, or senders to always ignore? **Threats to personal information (security alerts, breaches, identity theft) and money problems (failed payments, overdue bills, fraud); newsletters and promotions are never urgent.**
- [ ] 15. Which people should it know about from the start (family, close friends, birthdays)? *Deferred: not needed for now.*

**Trust and control**

- [x] 16. Which actions may it take without asking? **Reminders, list items, holds on your own calendar, and archiving or labeling email.**
- [x] 17. When away, may you approve an email send from Telegram? **No: drafts only until you're at the PC.**
- [ ] 18. Anything it must never do or touch: folders, accounts, contacts, topics?
- [x] 19. How long should transcripts be kept? **7 days.**

**Memory**

- [x] 20. Which vault? **A new vault just for TARS.**
- [x] 21. Remember automatically or only when told? **Automatically, saying "noted" each time.**
- [x] 22. Is the vault synced? **No.**

**Look and feel**

- [x] 23. On-screen presence? **Tray icon, with an overlay during conversations.**
- [x] 24. Visual style? **Modern Minimalist.**
- [x] 25. Light or dark? **Dark.**
- [x] 26. Animation? **Expressive.**
- [ ] 27. Should the overlay show your words as you speak, or only its replies?

**Budget and success**

- [x] 28. Monthly spend cap? **$25.**
- [ ] 29. After a month, what would make you say it works, and what would make you stop using it?

**Building it**

- [x] 30. How will you use the headset? **Bluetooth, worn most of the day; push-to-talk by default.**
- [x] 31. Should briefs and alerts run while the PC sleeps? **Yes: Windows wakes the PC for them.**
- [x] 32. What waits until you're back at the PC? **Anything involving other people.**
- [x] 33. How much of the coding will you do? **None: Claude builds and tests it on your PC.**
- [ ] 34. How many hours a week can you give to trying each phase and giving feedback?
- [ ] 35. Windows 10 or 11, and is it your everyday PC or a dedicated one?
- [ ] 36. More than one Google account, and are any calendars shared with others?
- [x] 37. About 25 phrases you'd actually say to TARS, for the test script. **See [TEST_SCRIPT.md](TEST_SCRIPT.md).**
- [ ] 38. Create accounts for the Anthropic API, Deepgram, ElevenLabs, Google Cloud, Todoist, a Telegram bot and Cloudflare (about an hour, guided).

## Sources

Checked 2026-10-07. Full research: Voice assistant tech stack 2026 report.

- Claude models overview and pricing
- Claude Agent SDK hosting and permissions
- Deepgram pricing and Flux quickstart
- ElevenLabs models and API pricing
- Pipecat on PyPI
- livekit-wakeword and openWakeWord
- Google OAuth testing-mode expiry and Gmail API scopes
- Todoist reminder limits
- Obsidian Sync troubleshooting
- Telegram Bot API and Telegram FAQ
- Google Maps pricing and Open-Meteo terms
