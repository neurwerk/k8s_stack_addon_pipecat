"""Streaming and real pinned WebRTC boundary regressions, without service calls."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pipecat.frames.frames import (
    AggregatedTextFrame,
    EndFrame,
    ErrorFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner
from pyee.asyncio import AsyncIOEventEmitter

from simple_chatbot.config import Speech
from simple_chatbot.services import HTTPTTS, bounded_pcm, bounded_sse
from simple_chatbot.webrtc import BrowserWebRTCConnection


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def event(content="Hello", *, finish=None):
    return {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}


def sse(*events, done=True):
    payload = "".join("data: " + json.dumps(value, ensure_ascii=False) + "\n\n" for value in events)
    return (payload + ("data: [DONE]\n\n" if done else "")).encode()


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_complete_event_is_delivered_before_another_network_read(self):
        next_read = asyncio.Event()

        class Gated(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse(event(), done=False)
                next_read.set()
                await asyncio.Event().wait()

        response = httpx.Response(
            200, stream=Gated(), headers={"content-type": "text/event-stream"}
        )
        stream = bounded_sse(response)
        try:
            first = await asyncio.wait_for(anext(stream), 0.5)
            self.assertEqual(first, event())
            self.assertFalse(next_read.is_set())
        finally:
            await stream.aclose()
            await response.aclose()

    async def test_incremental_utf8_crlf_multiline_and_usage(self):
        first = event("Héllo")
        # SSE allows a JSON event to be split into multiple data fields.
        text = json.dumps(first, ensure_ascii=False).replace('"choices": ', '"choices":\ndata: ')
        payload = ("data: " + text + "\r\n\r\n").encode()
        payload += sse(
            event(None, finish="stop"), {"choices": [], "usage": {"completion_tokens": 1}}
        )
        response = httpx.Response(
            200,
            stream=Chunks([bytes([byte]) for byte in payload]),
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )
        result = [value async for value in bounded_sse(response)]
        self.assertEqual(result[0], first)
        self.assertEqual(result[-1]["choices"], [])

    async def test_openrouter_terminal_usage_is_accounting_not_new_content(self):
        terminal = event("", finish="stop")
        terminal["choices"][0]["delta"]["role"] = "assistant"
        usage = {**terminal, "usage": {"completion_tokens": 3}}
        response = httpx.Response(
            200,
            stream=Chunks([sse(event("Hello"), terminal, usage)]),
            headers={"content-type": "text/event-stream"},
        )
        self.assertEqual(
            [value async for value in bounded_sse(response)], [event("Hello"), terminal, usage]
        )
        for delta in (
            {"content": "late"},
            {"reasoning_content": "late"},
            {"reasoning": "late"},
            {"tool_calls": [{"function": {"name": "late"}}]},
        ):
            bad_usage = {
                "choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 3},
            }
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                response = httpx.Response(
                    200,
                    content=sse(event(), terminal, bad_usage),
                    headers={"content-type": "text/event-stream"},
                )
                _ = [value async for value in bounded_sse(response)]

    async def test_malformed_empty_incomplete_and_unsuccessful_completions_fail(self):
        payloads = [
            sse(),
            sse(event(), done=False),
            sse(event()),
            sse(event("", finish="stop")),
            sse(event(), event(None, finish="length")),
            sse(event(), event(None, finish="content_filter")),
            sse(event(), event(None, finish="tool_calls")),
            sse({"choices": None}),
            sse({"choices": ["malformed"]}),
            sse({"choices": [{"index": 0, "delta": {"content": []}}]}),
            sse({"choices": []}),
            sse({"error": {"message": "synthetic"}}),
            sse(event(), event(None, finish="stop"), event("late")),
            sse(event(), event(None, finish="stop"), event("", finish="stop")),
            sse(
                event(),
                event(None, finish="stop"),
                {**event("", finish="length"), "usage": {}},
            ),
        ]
        for payload in payloads:
            response = httpx.Response(
                200, stream=Chunks([payload]), headers={"content-type": "text/event-stream"}
            )
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _ = [value async for value in bounded_sse(response)]
        response = httpx.Response(
            200,
            stream=Chunks([sse(event("ok", finish="stop"))]),
            headers={"content-type": "application/json"},
        )
        with self.assertRaises(ValueError):
            _ = [value async for value in bounded_sse(response)]

    async def test_pcm_streams_without_waiting_for_eof(self):
        second = asyncio.Event()

        class Gated(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"\x00\x01" * 16
                second.set()
                await asyncio.Event().wait()

        response = httpx.Response(200, stream=Gated(), headers={"content-type": "audio/pcm"})
        stream = bounded_pcm(response)
        try:
            self.assertEqual(await asyncio.wait_for(anext(stream), 0.5), b"\x00\x01" * 16)
            self.assertFalse(second.is_set())
        finally:
            await stream.aclose()
            await response.aclose()

    async def test_pcm_rejects_containers_errors_bad_types_and_odd_samples(self):
        payloads = [
            b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 40,
            b"ID3" + b"\0" * 40,
            b"OggS" + b"\0" * 40,
            b'{"error":"synthetic"}',
            b"<html>failure</html>",
        ]
        for payload in payloads:
            for boundary in range(1, min(16, len(payload))):
                response = httpx.Response(
                    200,
                    stream=Chunks([payload[:boundary], payload[boundary:]]),
                    headers={"content-type": "application/octet-stream"},
                )
                emitted = []
                with self.assertRaises(ValueError):
                    async for chunk in bounded_pcm(response):
                        emitted.append(chunk)
                self.assertEqual(emitted, [])
        for media, payload in [
            ("audio/wav", b"\0" * 40),
            ("application/json", b"\0" * 40),
            ("audio/pcm;rate=16000", b"\0" * 40),
            ("audio/pcm", b"\0" * 3),
            ("audio/pcm", b""),
        ]:
            response = httpx.Response(
                200, stream=Chunks([payload]), headers={"content-type": media}
            )
            with self.assertRaises(ValueError):
                _ = [chunk async for chunk in bounded_pcm(response)]
        response = httpx.Response(
            200,
            stream=Chunks([b"\0", b"\x01" * 5]),
            headers={"content-type": "audio/pcm;rate=24000;channels=1"},
        )
        self.assertEqual(
            b"".join([chunk async for chunk in bounded_pcm(response)]), b"\0" + b"\x01" * 5
        )


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.pcm = b"\x00\x01" * 32
        self.response = httpx.Response(200, content=self.pcm, headers={"content-type": "audio/pcm"})

        def respond(request):
            self.requests.append(request)
            return self.response

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.tts = HTTPTTS(
            Speech(base_url="https://speech.example.com/v1", model="tts-1"), self.http
        )
        self.tts.push_frame = AsyncMock()
        self.tts.append_to_audio_context = AsyncMock(wraps=self.tts.append_to_audio_context)

    async def asyncTearDown(self):
        await self.http.aclose()

    async def test_formatting_only_fragments_do_not_request_speech(self):
        for text in ("**", "*", "---", "-", "`", "```", " \n** ` -\t", "\n"):
            with self.subTest(text=text):
                await self.tts._handle_aggregated_text(
                    AggregatedTextFrame(text, "sentence"), "synthetic"
                )
                self.assertEqual(self.requests, [])
                self.tts.append_to_audio_context.assert_not_awaited()
                self.tts.push_frame.assert_not_awaited()
                self.assertTrue(self.tts.is_usable)

    async def test_speech_after_skipped_formatting_preserves_words_and_audio(self):
        await self.tts._handle_aggregated_text(AggregatedTextFrame("**", "sentence"), "skipped")
        for text, expected in (
            ("**Hallo!** Wie geht es _dir_?", "Hallo! Wie geht es dir?"),
            ("Hallo! 😊", "Hallo! 😊"),
        ):
            with self.subTest(text=text):
                self.requests.clear()
                self.tts.append_to_audio_context.reset_mock()
                await self.tts._handle_aggregated_text(
                    AggregatedTextFrame(text, "sentence"), "synthetic"
                )
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(json.loads(self.requests[0].content)["input"], expected)
                audio = [
                    call.args[1]
                    for call in self.tts.append_to_audio_context.await_args_list
                    if isinstance(call.args[1], TTSAudioRawFrame)
                ]
                self.assertEqual(b"".join(frame.audio for frame in audio), self.pcm)
                self.assertTrue(
                    all(
                        (frame.sample_rate, frame.num_channels, frame.context_id)
                        == (24000, 1, "synthetic")
                        for frame in audio
                    )
                )
                self.assertTrue(self.tts.is_usable)

    async def test_synthesis_errors_are_sanitized_and_only_configuration_is_permanent(self):
        private = "synthetic-private-upstream-detail"
        for status, payload in (
            (500, private.encode()),
            (200, b""),
            (401, private.encode()),
            (403, private.encode()),
            (404, private.encode()),
            (422, private.encode()),
        ):
            with self.subTest(status=status):
                self.response = httpx.Response(
                    status, content=payload, headers={"content-type": "audio/pcm"}
                )
                tts = HTTPTTS(self.tts.speech, self.http)
                tts.push_frame = AsyncMock()
                await tts._handle_aggregated_text(
                    AggregatedTextFrame("Hallo!", "sentence"), "synthetic"
                )
                errors = [
                    call.args[0]
                    for call in tts.push_frame.await_args_list
                    if isinstance(call.args[0], ErrorFrame)
                ]
                self.assertEqual([error.error for error in errors], ["Speech synthesis failed"])
                self.assertTrue(all(error.exception is None for error in errors))
                self.assertNotIn(private, str(errors))
                self.assertEqual(tts.is_usable, status in (500, 200))

    async def test_failed_streams_and_empty_responses_do_not_end_the_live_pipeline(self):
        class Collector(FrameProcessor):
            def __init__(self):
                super().__init__()
                self.frames = []
                self.audio_seen = asyncio.Event()
                self.turn_finished = asyncio.Event()

            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if direction == FrameDirection.DOWNSTREAM:
                    self.frames.append(frame)
                    if isinstance(frame, TTSAudioRawFrame):
                        self.audio_seen.set()
                    if isinstance(frame, LLMFullResponseEndFrame):
                        self.turn_finished.set()
                await self.push_frame(frame, direction)

        collector = Collector()
        partial = b"\x01\x02" * 32
        complete = b"\x03\x04" * 32

        class Broken(httpx.AsyncByteStream):
            def __init__(self, after_audio):
                self.after_audio = after_audio

            async def __aiter__(self):
                if self.after_audio:
                    yield partial
                    await asyncio.wait_for(collector.audio_seen.wait(), 2)
                raise httpx.RemoteProtocolError("synthetic-private-upstream-detail")

        failures = [
            httpx.Response(500),
            httpx.Response(200, content=b"", headers={"content-type": "audio/pcm"}),
            httpx.Response(200, stream=Broken(False), headers={"content-type": "audio/pcm"}),
            httpx.Response(200, stream=Broken(True), headers={"content-type": "audio/pcm"}),
            httpx.Response(422, json={"detail": {"code": "no_speakable_text"}}),
        ]
        responses = iter(
            failures
            + [httpx.Response(200, content=complete, headers={"content-type": "audio/pcm"})]
        )
        requests = []

        def respond(request):
            requests.append(request)
            return next(responses)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            tts = HTTPTTS(self.tts.speech, http)
            worker = PipelineWorker(
                Pipeline([tts, collector]),
                enable_rtvi=False,
                enable_turn_tracking=False,
                idle_timeout_secs=None,
                processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
            )
            errors = []

            @worker.event_handler("on_pipeline_error")
            async def on_error(worker, frame):
                errors.append(frame)

            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                for index in range(len(failures) + 1):
                    collector.turn_finished.clear()
                    await worker.queue_frames(
                        [
                            LLMFullResponseStartFrame(),
                            AggregatedTextFrame(f"Sentence {index}.", "sentence"),
                            LLMFullResponseEndFrame(),
                        ]
                    )
                    await asyncio.wait_for(collector.turn_finished.wait(), 3)
                    self.assertTrue(tts.is_usable)
                    self.assertFalse(running.done())
                await worker.queue_frame(EndFrame())
                await asyncio.wait_for(running, 3)
            finally:
                await runner.cancel()
                if not running.done():
                    await asyncio.wait_for(running, 3)

        self.assertEqual(len(requests), 6)  # No retry of already emitted audio.
        self.assertEqual(
            b"".join(
                frame.audio for frame in collector.frames if isinstance(frame, TTSAudioRawFrame)
            ),
            partial + complete,
        )
        self.assertEqual(
            [frame.text for frame in collector.frames if isinstance(frame, TTSTextFrame)],
            ["Sentence 5."],
        )
        self.assertEqual(sum(e.error == "Speech synthesis failed" for e in errors), 5)
        self.assertTrue(all(error.exception is None for error in errors))


class Channel(AsyncIOEventEmitter):
    readyState = "connecting"

    def close(self):
        self.readyState = "closed"

    def send(self, message):
        pass


class WebRTCBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_ready_is_denied_before_real_pending_queue(self):
        connection = BrowserWebRTCConnection(ice_servers=[])
        try:
            with patch.object(connection, "is_connected", return_value=False):
                channel = Channel()
                connection._pc.emit("datachannel", channel)
                channel.emit(
                    "message",
                    json.dumps(
                        {
                            "label": "rtvi-ai",
                            "id": "oversized",
                            "type": "client-ready",
                            "data": {"version": "1.0.0", "about": {"large": "x" * 4096}},
                        }
                    ),
                )
                await asyncio.sleep(0)
                self.assertEqual(connection._pending_app_messages, [])
                self.assertFalse(connection.browser_messages.ready)
        finally:
            await connection.disconnect()

    async def test_rate_limit_applies_before_real_signal_handler_even_when_tasks_drain(self):
        with patch("simple_chatbot.webrtc.time", SimpleNamespace(monotonic=lambda: 100.0)):
            connection = BrowserWebRTCConnection(ice_servers=[])
            statuses = []

            @connection.event_handler("track-status")
            async def track_status(connection, index, enabled):
                statuses.append((index, enabled))

            try:
                channel = Channel()
                connection._pc.emit("datachannel", channel)
                raw = json.dumps(
                    {
                        "type": "signalling",
                        "message": {"type": "trackStatus", "receiver_index": 0, "enabled": False},
                    }
                )
                for _ in range(100):
                    channel.emit("message", raw)
                    await asyncio.sleep(0)
                self.assertEqual(len(statuses), 16)
            finally:
                await connection.disconnect()

    async def test_real_pinned_channel_path_denies_rtvi_bypasses_and_bounds_pending_ready(self):
        ready = json.dumps(
            {
                "label": "rtvi-ai",
                "id": "ready",
                "type": "client-ready",
                "data": {"version": "1.0.0"},
            }
        )
        for connected in (False, True):
            connection = BrowserWebRTCConnection(ice_servers=[])
            received = []

            @connection.event_handler("app-message")
            async def app_message(connection, message, received=received):
                received.append(message)

            try:
                with patch.object(connection, "is_connected", return_value=connected):
                    channel = Channel()
                    connection._pc.emit("datachannel", channel)
                    channel.emit("message", ready)
                    for kind in (
                        "raw-audio",
                        "raw-audio-batch",
                        "send-text",
                        "send-file",
                        "function-call-result",
                    ):
                        channel.emit(
                            "message",
                            json.dumps(
                                {
                                    "label": "rtvi-ai",
                                    "id": "attack",
                                    "type": kind,
                                    "data": {"audio": ["unsafe"]},
                                }
                            ),
                        )
                    channel.emit("message", ready)
                    channel.emit("message", "x" * 4097)
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertLessEqual(len(connection.inbound_tasks), 16)
                    if connected:
                        self.assertEqual(received, [json.loads(ready)])
                        self.assertEqual(connection._pending_app_messages, [])
                    else:
                        self.assertEqual(connection._pending_app_messages, [json.loads(ready)])
                        self.assertEqual(received, [])
                    extra = Channel()
                    connection._pc.emit("datachannel", extra)
                    self.assertEqual(extra.readyState, "closed")
                    self.assertIs(connection._data_channel, channel)
            finally:
                await connection.disconnect()

    async def test_real_pinned_channel_path_preserves_bounded_transport_signalling(self):
        connection = BrowserWebRTCConnection(ice_servers=[])
        try:
            channel = Channel()
            connection._pc.emit("datachannel", channel)
            channel.emit("message", "ping: 1700000000000")
            channel.emit(
                "message",
                json.dumps(
                    {
                        "type": "signalling",
                        "message": {"type": "trackStatus", "receiver_index": 0, "enabled": False},
                    }
                ),
            )
            await asyncio.sleep(0)
            self.assertIsNotNone(connection._last_received_time)
            self.assertEqual(connection._pending_app_messages, [])
            for _ in range(100):
                channel.emit("message", "ping: 1700000000000")
            self.assertLessEqual(len(connection.inbound_tasks), 16)
        finally:
            await connection.disconnect()
