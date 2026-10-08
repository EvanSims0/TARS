"""The Pipecat processor that sits between speech-to-text and text-to-speech.

It hands finished user turns to the agent, streams the reply to the voice phrase
by phrase, and stops everything when the user talks over it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ProposedUserStartedSpeakingFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ..agent import Agent
from ..spend import SpendLedger
from .chunker import PhraseChunker

STOP_WORDS = {"stop", "cancel", "never mind", "nevermind", "be quiet", "quiet", "shush", "that's enough"}


def is_stop_command(text: str) -> bool:
    norm = re.sub(r"[^\w\s']", "", text.lower()).strip()
    norm = re.sub(r"^(tars|hey tars)\s+", "", norm)
    return norm in STOP_WORDS


class TarsBrain(FrameProcessor):
    def __init__(
        self,
        agent: Agent,
        ledger: SpendLedger,
        on_state: Callable[[str], None] = lambda s: None,
        on_user_speaking: Callable[[], None] = lambda: None,
        on_bot_done: Callable[[], None] = lambda: None,
    ):
        super().__init__()
        self.agent = agent
        self.ledger = ledger
        self.on_state = on_state
        self.on_user_speaking = on_user_speaking
        self.on_bot_done = on_bot_done
        self._task: asyncio.Task | None = None
        self._bot_speaking = False

    @property
    def busy(self) -> bool:
        return self._bot_speaking or (self._task is not None and not self._task.done())

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (ProposedUserStartedSpeakingFrame, UserStartedSpeakingFrame)):
            self.on_user_speaking()
            self.on_state("listening")
            if self.busy:
                await self.interrupt()
        elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
            await self._on_user_turn(frame.text)
            return
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self.on_state("speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self._task is None or self._task.done():
                self.on_state("needs_confirmation" if self.agent.gate.has_pending() else "idle")
                self.on_bot_done()

        await self.push_frame(frame, direction)

    async def interrupt(self) -> None:
        """Stop talking and drop the turn in progress (talking over it, or "stop")."""
        if self._task and not self._task.done():
            await self.cancel_task(self._task)
        self._task = None
        await self.broadcast_interruption()

    async def _on_user_turn(self, text: str) -> None:
        logger.info(f"You: {text}")
        if is_stop_command(text):
            self.agent.gate.cancel()
            await self.interrupt()
            self.on_state("idle")
            self.on_bot_done()
            return
        if self._task and not self._task.done():
            await self.cancel_task(self._task)
        self._task = self.create_task(self._respond(text))

    async def _respond(self, text: str) -> None:
        self.on_state("thinking")
        chunker = PhraseChunker()
        chars = 0

        async def speak(phrase: str) -> None:
            nonlocal chars
            if phrase:
                chars += len(phrase)
                await self.push_frame(LLMTextFrame(phrase + " "))

        async def on_text(delta: str) -> None:
            for phrase in chunker.feed(delta):
                await speak(phrase)

        await self.push_frame(LLMFullResponseStartFrame())
        try:
            result = await self.agent.handle(text, on_text)
            logger.info(f"TARS: {result.text}  [{result.first_text_ms} ms, ${result.usd:.4f}]")
        except Exception as e:
            logger.exception("turn failed")
            await speak(_plain_failure(e))
        await speak(chunker.flush())
        await self.push_frame(LLMFullResponseEndFrame())
        if chars:
            self.ledger.record_tts(chars)
        else:
            self.on_state("idle")
            self.on_bot_done()

    async def announce(self, text: str) -> None:
        """Speak something unprompted, such as a finished timer."""
        self.ledger.record_tts(len(text))
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))


def _plain_failure(error: Exception) -> str:
    name = type(error).__name__
    if "Connection" in name or "Timeout" in name:
        return "I can't reach Claude right now; check the internet connection."
    if "RateLimit" in name or "Overloaded" in name:
        return "Claude is busy right now; try again in a moment."
    if "Authentication" in name or "Permission" in name:
        return "My Claude API key was rejected; it needs updating in settings."
    return "Something went wrong on my side; try that again."
