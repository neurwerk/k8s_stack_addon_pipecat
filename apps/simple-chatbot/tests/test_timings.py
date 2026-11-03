"""Reviewed timing boundaries use synthetic media and a manually advanced clock."""

import asyncio
import io
import json
import math
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pipecat.frames.frames import (
    AggregatedTextFrame,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from test_security import settings
from test_streams import event, sse

from simple_chatbot.services import HTTPSTT, HTTPTTS, GatewayLLM
from simple_chatbot.timings import TurnTimings


def wav_audio(seconds):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(int(seconds * 16000 * 2)))
    return output.getvalue()


class TimingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.events = []
        self.timings = TurnTimings(AsyncMock(side_effect=self.events.append))
        clock = SimpleNamespace(monotonic=lambda: self.now)
        self.enterContext(patch("simple_chatbot.services.time", clock))
        self.enterContext(patch("simple_chatbot.timings.time", clock))
        self.config = settings(Path("unused"))

    async def test_stt_totals_are_consumed_once_and_snapshots_are_isolated(self):
        replies = iter(((0.25, "eins  zwei\ndrei vier"), (0.75, "fünf sechs")))

        def respond(request):
            duration, text = next(replies)
            self.now += duration
            return httpx.Response(200, json={"text": text})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            stt = HTTPSTT(self.config.stt, http, self.timings)
            for seconds in (2, 4):
                _ = [frame async for frame in stt.run_stt(wav_audio(seconds))]
        first = self.events[0].copy()
        turn = await self.timings.begin_llm()
        self.assertEqual(turn.values, {"stt_ms": 1000, "words": 6, "audio_seconds": 6})
        self.assertEqual(turn.kind, "reply")
        self.assertEqual((await self.timings.begin_llm()).values, {})
        self.assertEqual(self.events[0], first)
        other = TurnTimings(AsyncMock())
        greeting = await other.begin_llm()
        self.assertEqual((greeting.turn, greeting.kind, greeting.values), (1, "greeting", {}))
        self.assertIsNone(self.timings.pending)

    async def test_llm_first_text_excludes_headers_reasoning_and_controls(self):
        owner = self

        class DelayedText(httpx.AsyncByteStream):
            async def __aiter__(self):
                chunks = [
                    event(""),
                    {"choices": [{"index": 0, "delta": {"reasoning": "private-reasoning"}}]},
                    event("<think>private-control</think>"),
                    event("  "),
                    event("Hello."),
                    event(" More."),
                    event("", finish="stop"),
                ]
                for index, chunk in enumerate(chunks):
                    owner.now = 101 + index
                    if index <= 4:
                        self_missing = all("llm_first_text_ms" not in e for e in owner.events)
                        owner.assertTrue(self_missing)
                    yield sse(chunk, done=False)
                yield b"data: [DONE]\n\n"

        def respond(request):
            return httpx.Response(
                200, stream=DelayedText(), headers={"content-type": "text/event-stream"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            llm = GatewayLLM(self.config, http, "private-token", lambda _: None, self.timings)
            llm.push_frame = AsyncMock()
            await llm.process_frame(LLMContextFrame(LLMContext([])), FrameDirection.DOWNSTREAM)
        start = next(
            call.args[0]
            for call in llm.push_frame.await_args_list
            if isinstance(call.args[0], LLMFullResponseStartFrame)
        )
        self.assertEqual(start.metadata["turn_timing"].values["llm_first_text_ms"], 5000)
        self.assertEqual(len(self.events), 2)
        self.assertNotIn("private", json.dumps(self.events))

    async def test_tts_first_validated_pcm_and_later_sentences_do_not_reset(self):
        owner = self
        requests = []

        class DelayedPCM(httpx.AsyncByteStream):
            async def __aiter__(self):
                owner.now += 0.25
                yield b"\x00\x01" * 4
                owner.assertNotIn("tts_first_audio_ms", turn.values)
                owner.now += 0.75
                yield b"\x00\x01" * 16
                owner.now += 1
                yield b"\x00\x01" * 16

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                stream=DelayedPCM() if len(requests) == 1 else None,
                content=None if len(requests) == 1 else b"\x00\x01" * 32,
                headers={"content-type": "audio/pcm"},
            )

        turn = await self.timings.begin_llm()
        turn.llm_started = self.now
        self.now += 0.5
        await turn.first_text()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            tts = HTTPTTS(self.config.tts, http)
            tts.push_frame = AsyncMock()
            tts.append_to_audio_context = AsyncMock(wraps=tts.append_to_audio_context)
            start = LLMFullResponseStartFrame()
            start.metadata["turn_timing"] = turn
            await tts.process_frame(start, FrameDirection.DOWNSTREAM)
            await tts._handle_aggregated_text(AggregatedTextFrame("**", "sentence"))
            self.assertEqual(requests, [])
            self.assertNotIn("text_buffer_ms", turn.values)
            self.now += 0.25
            await tts._handle_aggregated_text(AggregatedTextFrame("Hallo!", "sentence"))
            expected = {
                "llm_first_text_ms": 500,
                "text_buffer_ms": 250,
                "tts_first_audio_ms": 1000,
                "text_to_audio_ms": 1250,
            }
            self.assertEqual(turn.values, expected)
            count = len(self.events)
            self.now += 10
            await tts._handle_aggregated_text(AggregatedTextFrame("Noch ein Satz.", "sentence"))
            self.assertEqual(turn.values, expected)
            self.assertEqual(len(self.events), count)
            self.assertTrue(
                any(
                    isinstance(call.args[1], TTSAudioRawFrame)
                    for call in tts.append_to_audio_context.await_args_list
                )
            )

    async def test_inflight_tts_keeps_its_turn_and_cancellation_preserves_partial_metrics(self):
        waiting = asyncio.Event()
        release = asyncio.Event()
        streams = []

        class GatedPCM(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                waiting.set()
                await release.wait()
                yield b"\x00\x01" * 32

            async def aclose(self):
                self.closed = True

        def respond(request):
            stream = GatedPCM()
            streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "audio/pcm"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            llm = GatewayLLM(self.config, http, "unused", lambda _: None, self.timings)
            tts = HTTPTTS(self.config.tts, http)
            tts.push_frame = AsyncMock()

            async def begin():
                # Use the same start-frame handoff as GatewayLLM, without another HTTP request.
                turn = await llm.timings.begin_llm()
                turn.llm_started = self.now
                self.now += 0.1
                await turn.first_text()
                start = LLMFullResponseStartFrame()
                start.metadata["turn_timing"] = turn
                await tts.process_frame(start, FrameDirection.DOWNSTREAM)
                return turn

            first = await begin()

            async def synthesize():
                return [frame async for frame in tts.run_tts("synthetic-private-text", "audio")]

            task = asyncio.create_task(synthesize())
            await asyncio.wait_for(waiting.wait(), 1)
            second = await begin()
            self.now += 1
            release.set()
            await asyncio.wait_for(task, 1)
            self.assertIn("tts_first_audio_ms", first.values)
            self.assertNotIn("tts_first_audio_ms", second.values)
            waiting.clear()
            release.clear()
            task = asyncio.create_task(synthesize())
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(streams[-1].closed)
            self.assertEqual(second.status, "interrupted")
            self.assertIn("llm_first_text_ms", second.values)
            self.assertNotIn("tts_first_audio_ms", second.values)
            await tts.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
            self.assertIsNone(tts.timing)

        allowed = {
            "type",
            "turn",
            "kind",
            "status",
            "stt_ms",
            "words",
            "audio_seconds",
            "llm_first_text_ms",
            "text_buffer_ms",
            "tts_first_audio_ms",
            "text_to_audio_ms",
        }
        for payload in self.events:
            self.assertLessEqual(payload.keys(), allowed)
            for key, value in payload.items():
                if key not in ("type", "kind", "status"):
                    self.assertTrue(math.isfinite(value) and value >= 0)
        self.assertNotIn("private", json.dumps(self.events))

    async def test_failed_request_has_no_fabricated_milestones(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(500, text="synthetic-private-failure")
            )
        ) as http:
            llm = GatewayLLM(self.config, http, "private-key", lambda _: None, self.timings)
            llm.push_frame = AsyncMock()
            await llm.process_frame(LLMContextFrame(LLMContext([])), FrameDirection.DOWNSTREAM)
        self.assertEqual(self.events[-1]["status"], "failed")
        self.assertNotIn("llm_first_text_ms", self.events[-1])
        self.assertNotIn("private", json.dumps(self.events))
