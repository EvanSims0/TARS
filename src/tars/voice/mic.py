"""Push-to-talk microphone that is only open while you're talking to TARS.

A Bluetooth headset drops to low-quality call audio whenever its mic is open,
so the input stream is opened on the talk hotkey and closed again once the
conversation goes quiet (about 8 seconds after TARS last spoke).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import StartFrame
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.local.audio import (
    LocalAudioInputTransport,
    LocalAudioOutputTransport,
    LocalAudioTransport,
    LocalAudioTransportParams,
)


class PushToTalkInput(LocalAudioInputTransport):
    async def setup(self, setup: FrameProcessorSetup):
        # Skip the parent's setup, which opens the mic for the whole session.
        await BaseInputTransport.setup(self, setup)

    async def start(self, frame: StartFrame):
        await BaseInputTransport.start(self, frame)
        await self.set_transport_ready(frame)

    @property
    def is_open(self) -> bool:
        return self._in_stream is not None

    def open_mic(self) -> None:
        if self._in_stream is not None:
            return
        self._in_stream = self._py_audio.open(
            format=self._py_audio.get_format_from_width(2),
            channels=self._params.audio_in_channels,
            rate=self.sample_rate,
            frames_per_buffer=int(self.sample_rate / 100) * 2,
            stream_callback=self._audio_in_callback,
            input=True,
            input_device_index=self._params.input_device_index,
        )
        self._in_stream.start_stream()

    def close_mic(self) -> None:
        if self._in_stream is None:
            return
        self._in_stream.stop_stream()
        self._in_stream.close()
        self._in_stream = None


class PushToTalkTransport(LocalAudioTransport):
    def input(self) -> PushToTalkInput:
        if not self._input:
            self._input = PushToTalkInput(self._pyaudio, self._params)
        return self._input  # type: ignore[return-value]

    def output(self) -> LocalAudioOutputTransport:
        return super().output()  # type: ignore[return-value]


class MicController:
    """Decides when the mic is open: hotkey opens it, a quiet follow-up window closes it."""

    def __init__(
        self,
        mic: PushToTalkInput,
        follow_up_seconds: float,
        on_state: Callable[[str], None],
        on_audio_seconds: Callable[[float], None],
        on_window: Callable[[float], None] = lambda closes_at: None,
    ):
        self.mic = mic
        self.follow_up = follow_up_seconds
        self.on_state = on_state
        self.on_audio_seconds = on_audio_seconds
        self.on_window = on_window  # when the follow-up window closes, for the overlay's countdown
        self.muted = False
        self._close_handle: asyncio.TimerHandle | None = None
        self._opened_at = 0.0

    def talk(self) -> None:
        """Talk hotkey pressed."""
        if self.muted:
            self.on_state("muted")
            return
        if not self.mic.is_open:
            self.mic.open_mic()
            self._opened_at = time.monotonic()
            logger.info("Mic open")
        self.on_state("listening")
        self.close_after_quiet()

    def user_speaking(self) -> None:
        self._cancel_close()
        self.on_window(0.0)

    def close_after_quiet(self) -> None:
        self._cancel_close()
        self._close_handle = asyncio.get_running_loop().call_later(self.follow_up, self.close)
        if self.mic.is_open:
            self.on_window(time.time() + self.follow_up)

    def close(self) -> None:
        self._cancel_close()
        self.on_window(0.0)
        if self.mic.is_open:
            self.mic.close_mic()
            self.on_audio_seconds(time.monotonic() - self._opened_at)
            logger.info("Mic closed")
            self.on_state("muted" if self.muted else "idle")

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        if self.muted:
            self.close()
        self.on_state("muted" if self.muted else "idle")

    def _cancel_close(self) -> None:
        if self._close_handle is not None:
            self._close_handle.cancel()
            self._close_handle = None


def transport(params: dict) -> PushToTalkTransport:
    return PushToTalkTransport(LocalAudioTransportParams(**params))


__all__ = ["MicController", "PushToTalkTransport", "PushToTalkInput", "transport"]
