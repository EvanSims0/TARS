"""Wire config, secrets, integrations and the agent together."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from . import secrets
from .actionlog import ActionLog
from .actions import ToolRegistry
from .agent import Agent, AnthropicBackend, Backend
from .config import Config
from .gate import ConfirmationGate
from .local_tools import PersonalityListener, Timers, UndoStack, memory_map_tool, personality_tool
from .local_tools import build_tools as local_tools
from .memory import Vault
from .mood import MoodMonitor
from .personality import Personality, PersonalitySettings
from .spend import SpendLedger, TurnLog
from .transcripts import Transcripts
from .ui.live import LiveState
from .ui.server import AppServer, UiContext


@dataclass
class App:
    config: Config
    agent: Agent
    timers: Timers
    vault: Vault
    transcripts: Transcripts
    ledger: SpendLedger
    turn_log: TurnLog
    personality: Personality
    mood: MoodMonitor
    live: LiveState
    actions: ActionLog
    # The local pages: overlay, History, Settings, Setup, Memory, Status.
    ui: AppServer
    # Called after a personality change, e.g. to switch the voice for the calm mode.
    personality_listeners: list[PersonalityListener] = field(default_factory=list)
    connected: dict[str, bool] = field(default_factory=dict)
    # Set by the desktop shell: bring the overlay up (e.g. to review a parked draft).
    show_overlay: Callable[[], None] | None = None


def build_app(
    config: Config,
    announce: Callable[[str], Awaitable[None]],
    backend: Backend | None = None,
) -> App:
    data = config.data_dir
    vault = Vault(config.vault)
    vault.ensure()
    transcripts = Transcripts(data, config.privacy.transcript_days)
    transcripts.purge()
    ledger = SpendLedger(data, config.spend)
    turn_log = TurnLog(data)
    undo = UndoStack()
    timers = Timers(announce)
    registry = ToolRegistry()
    registry.add(*local_tools(vault, transcripts, timers, undo))
    live = LiveState(config.voice.follow_up_seconds)
    actions = ActionLog(data, config.privacy.transcript_days)
    actions.purge()
    undo.on_undone = lambda undo_id: _mark_undone(actions, undo_id)
    ui = AppServer(UiContext(config, vault, live=live))  # started the first time a page opens
    registry.add(memory_map_tool(lambda: ui.open("memory")))
    defaults = config.personality
    personality = Personality.load(
        data / "personality.json",
        PersonalitySettings(humor=defaults.humor, bluntness=defaults.bluntness, trust=defaults.trust),
    )
    listeners: list[PersonalityListener] = []
    registry.add(personality_tool(personality, listeners))
    mood = MoodMonitor(personality)
    connected: dict[str, bool] = {}
    http = httpx.AsyncClient(timeout=15)

    from .integrations import places

    registry.add(*places.build_tools(places.Places(config.location, secrets.get_secret(secrets.GOOGLE_MAPS_API_KEY), http)))
    connected["Weather (Open-Meteo)"] = config.location.latitude is not None
    connected["Google Maps"] = bool(secrets.get_secret(secrets.GOOGLE_MAPS_API_KEY))

    if token := secrets.get_secret(secrets.TODOIST_API_TOKEN):
        from .integrations import todoist

        client = todoist.TodoistClient(token, http)
        registry.add(*todoist.build_tools(client, config.todoist))
        mood.open_tasks_due_today = lambda: todoist.open_due_today(client)
    connected["Todoist"] = bool(token)

    from .integrations.google_auth import GoogleSession

    session = GoogleSession.from_store(http)
    if session is not None:
        from .integrations import gcal, gmail

        calendar = gcal.Calendar(session, config.location.timezone)
        registry.add(*gcal.build_tools(calendar))
        mood.events = lambda start, end: calendar.events(start.isoformat(), end.isoformat())
        registry.add(*gmail.build_tools(gmail.Gmail(session)))
    connected["Google (Gmail, Calendar)"] = session is not None

    anthropic_key = secrets.get_secret(secrets.ANTHROPIC_API_KEY)
    connected["Claude"] = bool(anthropic_key) or backend is not None
    agent = Agent(
        backend=backend or AnthropicBackend(anthropic_key),
        registry=registry,
        gate=ConfirmationGate(data / "parked.json"),
        ledger=ledger,
        turn_log=turn_log,
        vault=vault,
        transcripts=transcripts,
        undo=undo,
        config=config.brain,
        timezone=config.location.timezone,
        user_name=config.user_name,
        user_email=config.user_email,
        instructions=config.instructions(),
        personality=personality,
        live=live,
        actions=actions,
    )
    app = App(config, agent, timers, vault, transcripts, ledger, turn_log, personality, mood, live, actions, ui,
              listeners, connected)
    ui.ctx.app = app
    return app


def _mark_undone(actions: ActionLog, undo_id: str) -> None:
    """A spoken "undo" crosses the action off in History too."""
    for action in actions.recent():
        if action.get("undo_id") == undo_id and not action["undone"]:
            actions.mark_undone(action["id"])
            return


def status_lines(app: App) -> list[str]:
    lines = ["Connected:"]
    lines += [f"  {'yes' if ok else 'no '}  {name}" for name, ok in app.connected.items()]
    summary = app.turn_log.summary()
    lines.append(f"Today: {summary['turns']} turns")
    if "first_word_p50_ms" in summary:
        lines.append(
            f"  first word: typical {summary['first_word_p50_ms']} ms, 9 in 10 under "
            f"{summary['first_word_p90_ms']} ms, {round(summary['under_3s_share'] * 100)}% under 3 s"
        )
    p = app.personality
    lines.append(f"Personality: {p.name}, humor {p.settings.humor}%, bluntness {p.settings.bluntness}%, "
                 f"trust {p.settings.trust}%")
    cap = app.config.spend.monthly_cap_usd
    lines.append(f"Spend: ${app.ledger.today_total():.2f} today, ${app.ledger.month_total():.2f} of ${cap:.0f} this month")
    parked = app.agent.gate.parked()
    if parked:
        lines.append(f"Waiting for you at the PC: {len(parked)} draft(s)")
    return lines
