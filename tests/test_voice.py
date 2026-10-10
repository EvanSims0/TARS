from __future__ import annotations

import pytest

pytest.importorskip("pipecat")

from tars.voice.brain import is_stop_command  # noqa: E402


@pytest.mark.parametrize("text", ["Stop.", "cancel", "TARS, stop", "never mind", "Hey TARS stop"])
def test_stop_commands(text):
    assert is_stop_command(text)


@pytest.mark.parametrize("text", ["stop the timer", "cancel my dentist appointment", "don't stop"])
def test_requests_mentioning_stop_go_to_the_agent(text):
    assert not is_stop_command(text)


def test_voice_pipeline_builds(tmp_path):
    pytest.importorskip("pyaudio")
    from conftest import FakeBackend

    from tars.app import build_app
    from tars.config import Config
    from tars.voice.run import build_voice

    config = Config(home=tmp_path)
    config.voice.tts_voice_id = "voice123"

    async def announce(text):
        pass

    app = build_app(config, announce, backend=FakeBackend())
    worker, brain, mic = build_voice(config, app, lambda s: None, "dg-key", "el-key")
    assert brain.agent is app.agent
    assert not mic.mic.is_open  # the mic stays closed until the talk hotkey


async def test_brain_streams_phrases_and_stop_interrupts(make_agent):
    from conftest import FakeBackend, reply, text
    from pipecat.frames.frames import (
        InterruptionFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TranscriptionFrame,
    )
    from pipecat.tests.utils import SleepFrame, run_test

    from tars.voice.brain import TarsBrain

    agent = make_agent(FakeBackend(reply(text("Your dentist is at three, and traffic looks light. Leave by two."))))
    brain = TarsBrain(agent, agent.ledger)
    down, _ = await run_test(
        brain,
        frames_to_send=[
            TranscriptionFrame("When do I leave for the dentist?", "user", "now"),
            SleepFrame(0.2),
            TranscriptionFrame("stop", "user", "now"),
            SleepFrame(0.1),
        ],
    )
    kinds = [type(f) for f in down]
    assert kinds[0] is LLMFullResponseStartFrame and LLMFullResponseEndFrame in kinds
    phrases = [f.text for f in down if isinstance(f, LLMTextFrame)]
    assert "".join(phrases).split() == "Your dentist is at three, and traffic looks light. Leave by two.".split()
    assert InterruptionFrame in kinds  # "stop" never reaches the model and halts speech
    assert agent.ledger.today_total() > 0  # Claude and TTS spend recorded


def test_log_file_is_written_under_data(tmp_path):
    from loguru import logger

    from tars.logs import log_to_dir

    sink = log_to_dir(tmp_path / "data" / "logs", "tars-voice")
    logger.info("hello log")
    logger.complete()
    logger.remove(sink)
    logs = list((tmp_path / "data" / "logs").glob("tars-voice-*.log"))
    assert logs and "hello log" in logs[0].read_text(encoding="utf-8")
