"""Reviewed streaming and real pinned WebRTC boundary regressions, without service calls."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pyee.asyncio import AsyncIOEventEmitter

from simple_chatbot.services import bounded_pcm, bounded_sse
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
