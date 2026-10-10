"""Run TARS by voice on the PC.

Pipeline: push-to-talk mic -> Deepgram Flux (detects end of turn) -> TarsBrain
(the agent) -> ElevenLabs Flash v2.5 -> speakers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.workers.runner import WorkerRunner

from .. import secrets
from ..app import build_app
from ..brief import BRIEF_REQUEST, BriefScheduler, mission_card
from ..config import Config
from ..ui import desktop
from .brain import TarsBrain
from .mic import MicController, transport

STATE_LABELS = {
    "idle": "Idle: press the talk hotkey",
    "listening": "Listening",
    "thinking": "Thinking",
    "speaking": "Speaking (talk over me to interrupt)",
    "needs_confirmation": "Waiting for your yes or no",
    "muted": "Muted: microphone off",
    "problem": "Problem: a service is down",
}


def _require(name: str) -> str:
    value = secrets.get_secret(name)
    if not value:
        raise SystemExit(f"{name} is not set. Run `tars set-key {name}` first.")
    return value


def voice_for(config: Config, personality) -> str:
    """The calm mode can have its own voice; otherwise both modes share TARS's voice."""
    if personality.settings.calm and config.voice.calm_voice_id:
        return config.voice.calm_voice_id
    return config.voice.tts_voice_id


def build_voice(config: Config, app, show_state: Callable[[str], None], deepgram_key: str, elevenlabs_key: str):
    """Assemble the pipeline; returns (worker, brain, mic)."""
    audio = transport({
        "audio_in_enabled": True,
        "audio_out_enabled": True,
        "audio_in_sample_rate": config.voice.sample_rate,
        "input_device_index": config.voice.input_device_index,
        "output_device_index": config.voice.output_device_index,
    })
    mic = MicController(audio.input(), config.voice.follow_up_seconds, show_state, app.ledger.record_stt,
                        on_window=app.live.mic_window)
    stt = DeepgramFluxSTTService(
        api_key=deepgram_key,
        mip_opt_out=True,  # stay out of Deepgram's model-improvement program
        settings=DeepgramFluxSTTService.Settings(model=config.voice.stt_model, keyterm=["TARS"]),
    )
    tts = ElevenLabsTTSService(
        api_key=elevenlabs_key,
        settings=ElevenLabsTTSService.Settings(voice=voice_for(config, app.personality), model=config.voice.tts_model),
        # TarsBrain already sends speakable phrases; don't wait for whole sentences.
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )
    brain = TarsBrain(
        app.agent, app.ledger,
        on_state=show_state,
        on_user_speaking=mic.user_speaking,
        on_bot_done=mic.close_after_quiet,
    )
    async def switch_voice(personality) -> None:
        await brain.set_voice(ElevenLabsTTSService.Settings(voice=voice_for(config, personality)))

    if config.voice.calm_voice_id:
        app.personality_listeners.append(switch_voice)

    pipeline = Pipeline([audio.input(), stt, brain, tts, audio.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=config.voice.sample_rate, enable_metrics=True),
        idle_timeout_secs=None,
    )
    return worker, brain, mic


def show_state(state: str) -> None:
    # The on-screen indicator; the tray icon and overlay arrive in a later phase.
    print(f"\r[TARS] {STATE_LABELS.get(state, state):<45}", end="", flush=True)


async def run_voice(config: Config) -> None:
    from pynput import keyboard

    if not config.voice.tts_voice_id:
        raise SystemExit("Pick an ElevenLabs voice and set voice.tts_voice_id in config.toml.")

    loop = asyncio.get_running_loop()
    brain_ref: dict[str, TarsBrain] = {}

    async def announce(text: str) -> None:
        await brain_ref["brain"].announce(text)

    app = build_app(config, announce)

    def state(name: str) -> None:
        show_state(name)
        app.live.set_state(name)

    worker, brain, mic = build_voice(
        config, app, state, _require(secrets.DEEPGRAM_API_KEY), _require(secrets.ELEVENLABS_API_KEY),
    )
    brain_ref["brain"] = brain

    def on_talk() -> None:
        def press() -> None:
            mic.talk()
            if brain.busy:  # pressing the hotkey while TARS talks interrupts it
                asyncio.ensure_future(brain.interrupt())
        loop.call_soon_threadsafe(press)

    hotkeys = keyboard.GlobalHotKeys({
        config.voice.push_to_talk_key: on_talk,
        config.voice.mute_key: lambda: loop.call_soon_threadsafe(mic.toggle_mute),
    })
    hotkeys.start()

    # The overlay's buttons and the tray menu act through the same paths as the hotkeys and your voice.
    ctx = app.ui.ctx
    ctx.loop = loop
    ctx.submit = lambda text: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(brain.typed(text)))
    ctx.controls.update({
        "talk": on_talk,
        "mute": lambda: loop.call_soon_threadsafe(mic.toggle_mute),
        "quit": lambda: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(worker.cancel())),
    })
    app.ui.start()
    shell = desktop.launch(app.ui)

    for name, ok in app.connected.items():
        logger.info(f"{'connected' if ok else 'not set up'}: {name}")
    if parked := app.agent.gate.parked():
        logger.info(f"{len(parked)} draft(s) from the phone are waiting for you.")
    state("idle")

    async def deliver_brief() -> None:
        await brain.start_turn(BRIEF_REQUEST)
        asyncio.create_task(_brief_card(app))

    brief = BriefScheduler(
        config.brief, config.alerts, config.data_dir / "last-brief.txt",
        deliver=deliver_brief,
        busy=lambda: brain.busy or mic.mic.is_open,
    )

    runner = WorkerRunner()
    await runner.add_workers(worker)

    @worker.event_handler("on_pipeline_started")
    async def _start_brief(worker, frame):
        brief_task.append(asyncio.create_task(brief.run()))
        brief_task.append(asyncio.create_task(app.mood.run()))

    brief_task: list[asyncio.Task] = []
    try:
        await runner.run()
    finally:
        for task in brief_task:
            task.cancel()
        hotkeys.stop()
        mic.close()
        if shell is not None:
            shell.terminate()
        app.ui.stop()


async def _brief_card(app) -> None:
    """Fill the overlay's mission checklist once the brief's turn has started."""
    for _ in range(30):
        if app.live.snapshot()["brief"]:
            break
        await asyncio.sleep(0.1)
    try:
        app.live.show_card(await mission_card(app))
    except Exception as e:  # the spoken brief goes ahead without the card
        logger.debug(f"brief card: {e}")
