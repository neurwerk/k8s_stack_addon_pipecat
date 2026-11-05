"""Synthetic pipeline control regressions; no live microphones or model calls."""

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pipecat.frames.frames import InputAudioRawFrame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessor
from test_security import settings

from simple_chatbot.bot import run_bot


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_waits_for_transcription_and_interruption_before_ack(self):
        started = asyncio.Event()
        release = asyncio.Event()
        acknowledgements = []
        gates = []
        previous_context = LLMContext([{"role": "user", "content": "earlier turn"}])
        profiles = object()

        class FakeTransport:
            def input(self):
                return FrameProcessor()

            def output(self):
                return FrameProcessor()

            def event_handler(self, name):
                return lambda callback: callback

        class FakeRTVI:
            def __init__(self):
                self.handlers = {}

            def event_handler(self, name):
                def register(callback):
                    self.handlers[name] = callback
                    return callback

                return register

            async def interrupt_bot(self):
                started.set()
                await release.wait()

            async def send_server_message(self, data):
                acknowledgements.append(data)

        class FakeWorker:
            def __init__(self, pipeline, **kwargs):
                self.pipeline = pipeline
                self.rtvi = FakeRTVI()
                self.handlers = {}
                gates.extend(p for p in pipeline.processors if type(p).__name__ == "PauseGate")

            def event_handler(self, name):
                return lambda callback: self.handlers.setdefault(name, callback)

        class FakeRunner:
            async def add_workers(self, worker):
                self.worker = worker

            async def cancel(self):
                pass

            async def run(self):
                handle = self.worker.rtvi.handlers["on_client_message"]

                def message(action, sequence):
                    return SimpleNamespace(
                        type="conversation-control",
                        data={"action": action, "sequence": sequence},
                    )

                pausing = asyncio.create_task(handle(self.worker.rtvi, message("pause", 2)))
                await asyncio.wait_for(started.wait(), 1)
                self.assert_paused()
                resuming = asyncio.create_task(handle(self.worker.rtvi, message("resume", 3)))
                await asyncio.sleep(0)
                assert not acknowledgements
                release.set()
                await asyncio.gather(pausing, resuming)
                await handle(self.worker.rtvi, message("pause", 2))  # stale
                await handle(self.worker.rtvi, message("resume", 4))  # idempotent
                stt.pause_input.side_effect = RuntimeError("synthetic stop failure")
                try:
                    await handle(self.worker.rtvi, message("pause", 5))
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Failed pause must not be acknowledged")
                await handle(self.worker.rtvi, message("resume", 6))
                self.assert_paused()

            def assert_paused(self):
                assert all(gate.paused for gate in gates)

        user = FrameProcessor()
        user.reset = AsyncMock()
        strategy = SimpleNamespace(handle_user_turn_stopped=AsyncMock())
        controller = SimpleNamespace(
            stop=AsyncMock(),
            start=AsyncMock(),
            _user_speaking=True,
            _user_turn=True,
            user_turn_strategies=SimpleNamespace(stop=[strategy]),
        )
        user._user_turn_controller = controller
        model = SimpleNamespace(reset_states=Mock())
        vad_analyzer = SimpleNamespace(_vad_state=None, _model=model)
        user._vad_controller = SimpleNamespace(_vad_state=None, _vad_analyzer=vad_analyzer)
        user._full_user_turn_aggregation = "unfinished"
        user._user_turn_interrupted_bot = True
        user._user_turn_start_timestamp = "earlier"
        stt = FrameProcessor()

        async def pause_input():
            pass

        stt.pause_input = AsyncMock(side_effect=pause_input)
        stt.resume_input = Mock()
        profile_store = SimpleNamespace(profiles=profiles, attach=AsyncMock(), reset=AsyncMock())
        runner = FakeRunner()
        llm_factory = Mock(return_value=FrameProcessor())
        http = httpx.AsyncClient()
        with (
            patch("simple_chatbot.bot.SmallWebRTCTransport", return_value=FakeTransport()),
            patch("simple_chatbot.bot.HTTPSTT", return_value=stt),
            patch("simple_chatbot.bot.GatewayLLM", llm_factory),
            patch("simple_chatbot.bot.HTTPTTS", return_value=FrameProcessor()),
            patch(
                "simple_chatbot.bot.LLMContextAggregatorPair", return_value=(user, FrameProcessor())
            ),
            patch("simple_chatbot.bot.SileroVADAnalyzer"),
            patch("simple_chatbot.bot.LocalSmartTurnAnalyzerV3"),
            patch("simple_chatbot.bot.PipelineWorker", FakeWorker),
            patch("simple_chatbot.bot.WorkerRunner", return_value=runner),
        ):
            await run_bot(
                object(),
                settings(Path("unused")),
                http,
                "synthetic",
                lambda _: None,
                diarization=profile_store,
            )
        await http.aclose()

        self.assertEqual([entry["paused"] for entry in acknowledgements], [True, False, False])
        self.assertIs(llm_factory.call_args.kwargs["profiles"], profiles)
        self.assertIsNone(user._full_user_turn_aggregation)
        self.assertFalse(controller._user_turn)
        self.assertEqual(user._vad_controller._vad_state.name, "QUIET")
        self.assertEqual(stt.pause_input.await_count, 3)
        stt.resume_input.assert_called_once()
        self.assertEqual(len(gates), 2)
        for gate in gates:
            gate.paused = True
            gate.push_frame = AsyncMock()
            frame = (
                LLMContextFrame(previous_context)
                if gate.llm_boundary
                else InputAudioRawFrame(b"\0\0", 16000, 1)
            )
            from pipecat.processors.frame_processor import FrameDirection

            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
            gate.push_frame.assert_not_awaited()
