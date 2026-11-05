"""Reviewed speaker isolation, timeline and reconnection boundaries; synthetic audio only."""

import asyncio
import io
import json
import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner
from test_security import FakeConnection, settings
from websockets.asyncio.server import serve

from simple_chatbot.activity import Activity as CallActivity
from simple_chatbot.activity import ActivityObserver
from simple_chatbot.config import Diarization
from simple_chatbot.diarization import (
    Activity,
    DiarizationError,
    DiarizationSession,
    SpeakerSpan,
)
from simple_chatbot.server import create_app
from simple_chatbot.services import HTTPSTT
from simple_chatbot.speaker_transcripts import attribute_transcript
from simple_chatbot.speakers import SpeakerProfiles
from simple_chatbot.timings import TurnTimings


class AttributionTests(unittest.TestCase):
    def test_word_alignment_splits_turn_and_does_not_enroll_overlap(self):
        profiles = SpeakerProfiles(names={"speaker_0": "Alex"})
        span = SpeakerSpan(
            16000,
            48000,
            0,
            (
                Activity(16000, 32000, "speaker_0"),
                Activity(32000, 48000, "speaker_1"),
                Activity(40000, 48000, "speaker_0"),
            ),
        )
        payload = {
            "text": "Hello there everyone",
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.5},
                {"word": "there", "start": 1.0, "end": 1.4},
                {"word": "everyone", "start": 1.5, "end": 2.0},
            ],
        }
        text, speaker = attribute_transcript(payload, span, profiles)
        parts = json.loads(text)["speaker_turn"]
        self.assertEqual([p["speaker"] for p in parts], ["0:speaker_0", "0:speaker_1", "Unknown"])
        self.assertEqual(parts[0]["name"], "Alex")
        self.assertEqual(speaker, "")
        self.assertFalse(profiles.register("Bea", profiles.turn, profiles.generation)["ok"])

    def test_invalid_or_missing_timestamps_preserve_text_without_guessing(self):
        span = SpeakerSpan(0, 16000, 0, (Activity(0, 16000, "speaker_0"),))
        for words in (
            None,
            [],
            [{"word": "Hello", "start": 0, "end": float("nan")}],
            [{"word": "Hello", "start": 0, "end": 0.5}],
        ):
            text, speaker = attribute_transcript(
                {"text": "Hello everyone", "words": words}, span, SpeakerProfiles()
            )
            self.assertEqual(speaker, "")
            self.assertEqual(json.loads(text)["speaker_turn"][0]["text"], "Hello everyone")

    def test_forget_and_stale_turn_cannot_attach_old_name_to_new_voice(self):
        profiles = SpeakerProfiles()
        profiles.observe(["speaker_0"])
        generation, turn = profiles.generation, profiles.turn
        self.assertTrue(profiles.register("Alex", turn, generation)["ok"])
        profiles.observe(["speaker_1"])
        self.assertFalse(profiles.register("Alex", turn, generation)["ok"])
        profiles.clear()
        profiles.observe(["speaker_0"])
        self.assertFalse(profiles.register("Alex", profiles.turn, generation)["ok"])
        old = SpeakerSpan(0, 16000, generation, (Activity(0, 16000, "speaker_0"),))
        _, speaker = attribute_transcript(
            {
                "text": "Hi",
                "words": [
                    {"word": "Hi", "start": 0.1, "end": 0.6},
                ],
            },
            old,
            profiles,
        )
        self.assertEqual(speaker, "")
        self.assertEqual(profiles.names, {})


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.key = Path(self.directory.name) / "key"
        self.key.write_text("synthetic-service-key")
        self.states = {}
        self.connection_count = 0
        self.drop_ack = False
        self.drop_flush = False
        self.overlap = True
        self.audio_received = asyncio.Event()
        self.audio_release = asyncio.Event()
        self.audio_release.set()
        self.replay_gap = False
        self.packets = []
        self.server = await serve(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.config = Diarization(
            url=f"ws://127.0.0.1:{port}/v1/diarization/stream",
            key={"file": str(self.key)},
            timeout_seconds=1,
        )
        self.client = DiarizationSession(self.config)

    async def asyncTearDown(self):
        await self.client.delete()
        self.server.close()
        await self.server.wait_closed()
        self.directory.cleanup()

    async def handle(self, socket):
        self.assertEqual(socket.request.headers["Authorization"], "Bearer synthetic-service-key")
        self.connection_count += 1
        message = json.loads(await socket.recv())
        if message["type"] == "start":
            self.assertEqual(message["buffer_profile"], "ultra_low_latency")
            sid = f"session-{self.connection_count}"
            state = {"next": 0, "finalized": 0, "events": []}
            self.states[sid] = state
            reply = "started"
        else:
            sid = message["session_id"]
            if sid not in self.states or self.replay_gap:
                await socket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "replay_gap" if self.replay_gap else "session_expired",
                        }
                    )
                )
                return
            state = self.states[sid]
            reply = "resumed"
        await socket.send(
            json.dumps(
                {
                    "type": reply,
                    "session_id": sid,
                    "next_sample": state["next"],
                    "last_result_seq": len(state["events"]),
                    "finalized_through_sample": state["finalized"],
                    "expires_in_s": 86400,
                }
            )
        )
        if reply == "resumed":
            for event in state["events"][message["after_result_seq"] :]:
                await socket.send(json.dumps(event))
        async for raw in socket:
            if isinstance(raw, bytes):
                self.audio_received.set()
                await self.audio_release.wait()
                if socket.close_code is not None:
                    return
                start = struct.unpack("<Q", raw[:8])[0]
                self.assertEqual(start, state["next"])
                self.assertLessEqual(len(raw) - 8, 5120 * 2)
                state["next"] += (len(raw) - 8) // 2
                self.packets.append((sid, start, state["next"]))
                if self.drop_ack:
                    self.drop_ack = False
                    await socket.close()
                    return
                await socket.send(json.dumps({"type": "ack", "next_sample": state["next"]}))
                continue
            message = json.loads(raw)
            if message["type"] == "delete":
                self.states.pop(sid)
                await socket.send('{"type":"deleted"}')
                return
            self.assertEqual(message["type"], "flush")
            end = message["through_sample"]
            self.assertEqual(end, state["next"])
            if state["finalized"] < end:
                # Both final entries cover the same samples. The first must not
                # finalize the range before the second overlapping entry arrives.
                speakers = (
                    ("speaker_0", "speaker_1")
                    if self.overlap
                    else (f"speaker_{state['finalized'] // 16000 % 8}",)
                )
                for speaker in speakers:
                    event = {
                        "type": "activity",
                        "seq": len(state["events"]) + 1,
                        "session_id": sid,
                        "start_sample": state["finalized"],
                        "end_sample": end,
                        "speaker_id": speaker,
                        "state": "final",
                        "finalized_through_sample": end,
                    }
                    state["events"].append(event)
                    await socket.send(json.dumps(event))
            state["finalized"] = end
            if self.drop_flush:
                self.drop_flush = False
                await socket.close()
                return
            await socket.send(
                json.dumps(
                    {
                        "type": "flushed",
                        "request_id": message["request_id"],
                        "next_sample": end,
                        "finalized_through_sample": end,
                    }
                )
            )

    async def test_disconnect_resumes_voice_state_and_absolute_timeline(self):
        await self.client.attach()
        self.client.feed(bytes(16000 * 2))
        span = await self.client.flush(0)
        self.assertEqual(span.speaker(0, 16000), None)
        self.client.profiles.names["speaker_0"] = "Alex"
        sid = self.client.session_id
        await self.client.detach()
        await self.client.attach()
        self.client.feed(bytes(320 * 2))
        await self.client.flush(16000)
        self.assertEqual(self.client.session_id, sid)
        self.assertEqual(self.client.profiles.names, {"speaker_0": "Alex"})
        self.assertEqual(self.packets[-1][1:], (16000, 16320))

    async def test_lost_ack_does_not_duplicate_accepted_audio(self):
        self.drop_ack = True
        await self.client.attach()
        self.client.feed(bytes(640))
        await self.client.flush(0)
        self.assertEqual(len(self.packets), 1)
        self.assertEqual(self.connection_count, 2)

    async def test_lost_flush_reply_replays_results_without_losing_overlap(self):
        self.drop_flush = True
        await self.client.attach()
        activity = CallActivity()
        self.client.activity = activity
        self.audio_release.clear()
        self.client.feed(bytes(640))
        await asyncio.wait_for(self.audio_received.wait(), 1)
        now = 10.0
        clock = SimpleNamespace(monotonic=lambda: now)
        connect = self.client._connect

        async def reconnect():
            nonlocal now
            now += 0.5
            await connect()

        with (
            patch("simple_chatbot.diarization.time", clock),
            patch.object(self.client, "_connect", side_effect=reconnect),
        ):
            flushing = self.client.flush(0)
            self.assertEqual(activity.states["diarization"]["status"], "active")
            now += 2
            self.audio_release.set()
            span = await asyncio.wait_for(flushing, 3)
        self.assertEqual(span.finalization_seconds, 2.5)
        self.assertFalse(activity.operations["diarization"])
        self.assertEqual({a.speaker for a in span.activities}, {"speaker_0", "speaker_1"})
        self.assertIsNone(span.speaker(0, 320))
        self.assertEqual(self.client.sequence, 2)

    async def test_canceled_detach_cannot_resume_a_gapped_audio_timeline(self):
        await self.client.attach()
        old_id = self.client.session_id
        self.client.profiles.names["speaker_0"] = "Alex"
        self.audio_release.clear()
        self.client.feed(bytes(640))
        await asyncio.wait_for(self.audio_received.wait(), 1)
        flushing = asyncio.Event()
        original_flush = self.client.flush

        def flush(start):
            future = original_flush(start)
            flushing.set()
            return future

        with patch.object(self.client, "flush", side_effect=flush):
            closing = asyncio.create_task(self.client.detach())
            await asyncio.wait_for(flushing.wait(), 1)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
        self.audio_release.set()
        self.assertTrue(self.client.failed)
        await self.client.attach()
        self.assertNotEqual(self.client.session_id, old_id)
        self.assertEqual(self.client.profiles.names, {})
        self.client.feed(bytes(640))
        span = await self.client.flush(320)
        self.assertEqual((span.start, span.end), (320, 640))
        self.assertFalse(self.client.failed)

    async def test_real_segmented_pipeline_aligns_wav_preroll_and_excludes_padding(self):
        self.overlap = False

        class Collector(FrameProcessor):
            def __init__(self):
                super().__init__()
                self.transcripts = asyncio.Queue()

            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
                    await self.transcripts.put(frame)
                await self.push_frame(frame, direction)

        requests = []

        def respond(request):
            body = request.content
            self.assertIn(b"verbose_json", body)
            self.assertIn(b"timestamp_granularities[]", body)
            wav_start = body.index(b"RIFF")
            with wave.open(io.BytesIO(body[wav_start:]), "rb") as wav:
                requests.append(wav.getnframes())
            return httpx.Response(
                200,
                json={
                    "text": "Hello",
                    "words": [
                        {"word": "Hello", "start": 0.1, "end": 0.3},
                    ],
                },
            )

        await self.client.attach()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            activity = CallActivity()
            self.client.activity = activity
            timings = TurnTimings(AsyncMock(), activity)
            stt = HTTPSTT(settings(self.key).stt, http, timings, diarization=self.client)
            collector = Collector()
            worker = PipelineWorker(
                Pipeline([stt, collector]),
                params=PipelineParams(audio_in_sample_rate=16000),
                enable_rtvi=False,
                enable_turn_tracking=False,
                idle_timeout_secs=None,
                observers=[ActivityObserver(activity)],
            )
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                for index in range(2):
                    await worker.queue_frames(
                        [
                            VADUserStartedSpeakingFrame(),
                            InputAudioRawFrame(bytes(16000 * 2), 16000, 1),
                            VADUserStoppedSpeakingFrame(),
                        ]
                    )
                    frame = await asyncio.wait_for(collector.transcripts.get(), 3)
                    self.assertEqual(json.loads(frame.text)["speaker_turn"][0]["text"], "Hello")
                    self.assertEqual(
                        json.loads(frame.text)["speaker_turn"][0]["speaker"],
                        f"0:speaker_{index}",
                    )
                    self.assertTrue(frame.finalized)
                    self.assertEqual(activity.states["attribution"]["status"], "complete")
                    self.assertTrue(activity.transcript_ready)
                    self.assertIn("diarization_finalize_ms", timings.pending.values)
                await worker.queue_frame(EndFrame())
                await asyncio.wait_for(running, 3)
            finally:
                await runner.cancel()
                if not running.done():
                    await asyncio.wait_for(running, 3)
        # STT receives half a second of artificial trailing silence per segment;
        # the diarizer receives only the real microphone timeline.
        self.assertEqual(requests, [24000, 24000])
        self.assertEqual(self.client.position, 32000)
        self.assertEqual(self.packets[-1][2], 32000)

    async def test_forget_deletes_remote_state_and_changes_generation(self):
        await self.client.attach()
        self.client.feed(bytes(640))
        old_span = await self.client.flush(0)
        old_id = self.client.session_id
        self.client.profiles.names["speaker_0"] = "Alex"
        await self.client.reset()
        self.assertNotIn(old_id, self.states)
        self.assertNotEqual(old_id, self.client.session_id)
        self.assertNotEqual(old_span.generation, self.client.profiles.generation)
        self.client.feed(bytes(640))
        span = await self.client.flush(320)
        self.assertEqual((span.start, span.end), (320, 640))
        self.assertEqual(self.packets[-1][1:], (0, 320))
        self.assertEqual(self.client.profiles.names, {})

    async def test_restart_or_inactivity_discards_old_profiles(self):
        for expire_locally in (False, True):
            await self.client.attach()
            old_id = self.client.session_id
            self.client.profiles.names["speaker_0"] = "Alex"
            await self.client.detach()
            if expire_locally:
                self.client.profiles.updated -= 86401
            else:
                self.states.clear()
            await self.client.attach()
            self.assertNotEqual(old_id, self.client.session_id)
            self.assertEqual(self.client.profiles.names, {})
            await self.client.detach()

    async def test_replay_gap_and_malformed_sequence_fail_without_reassigning_names(self):
        await self.client.attach()
        self.client.profiles.names["speaker_0"] = "Alex"
        await self.client.detach()
        self.replay_gap = True
        with self.assertRaisesRegex(DiarizationError, "replay_gap"):
            await self.client.attach()
        self.replay_gap = False
        with self.assertRaises(DiarizationError):
            await self.client._event({"type": "ack", "next_sample": 999})
        with self.assertRaises(DiarizationError):
            await self.client._event(
                {"type": "activity", "seq": 2, "session_id": self.client.session_id}
            )


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_names_survive_conversation_reset_but_not_logout_or_another_browser(self):
        config = settings(Path("unused"), protected=True)
        config.diarization = Diarization(
            url="ws://speech.example.com:8010/v1/diarization/stream", key={"file": "unused"}
        )
        app = create_app(config)
        received = asyncio.Queue()

        async def bot(*args, diarization):
            await received.put(diarization)
            await asyncio.Event().wait()

        with (
            patch("simple_chatbot.server.Auth.initialize", new=AsyncMock()),
            patch("simple_chatbot.server.SmallWebRTCConnection", FakeConnection),
        ):
            async with app.router.lifespan_context(app):
                app.state.run_bot = bot
                transport = httpx.ASGITransport(app)
                async with (
                    httpx.AsyncClient(transport=transport, base_url=config.public_origin) as a,
                    httpx.AsyncClient(transport=transport, base_url=config.public_origin) as b,
                ):
                    for client in (a, b):
                        session = app.state.auth.create("token", "same-user", time.time() + 300)
                        client.cookies.set(app.state.auth.cookie, session.id)
                        client.headers.update(
                            {"origin": config.public_origin, "x-csrf-token": session.csrf}
                        )
                    offer = {"sdp": "synthetic", "type": "offer"}
                    response = await a.post("/example/api/offer", json=offer)
                    first = await asyncio.wait_for(received.get(), 1)
                    first.profiles.names["speaker_0"] = "Alex"
                    retiring, release = asyncio.Event(), asyncio.Event()

                    async def slow_detach():
                        retiring.set()
                        await release.wait()

                    with patch.object(first, "detach", side_effect=slow_detach):
                        closing = asyncio.create_task(
                            a.delete("/example/api/connections/" + response.json()["pc_id"])
                        )
                        await asyncio.wait_for(retiring.wait(), 1)
                        rejected = await a.post("/example/api/offer", json=offer)
                        self.assertEqual(rejected.status_code, 409)
                        release.set()
                        self.assertEqual((await closing).status_code, 200)
                    await a.post("/example/api/offer", json=offer)
                    resumed = await asyncio.wait_for(received.get(), 1)
                    self.assertIs(first, resumed)
                    self.assertEqual(resumed.profiles.names, {"speaker_0": "Alex"})
                    await b.post("/example/api/offer", json=offer)
                    other = await asyncio.wait_for(received.get(), 1)
                    self.assertIsNot(first, other)
                    self.assertEqual(other.profiles.names, {})
                    response = await a.post("/example/auth/logout")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(first.profiles.names, {})
                    self.assertEqual(
                        (await a.post("/example/api/offer", json=offer)).status_code, 401
                    )
                    await b.post("/example/api/cancel")
