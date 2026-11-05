"""Reviewed call isolation, observable concurrency and metadata boundaries."""

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMContextFrame,
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner
from test_security import settings
from test_streams import Chunks, event, sse
from test_timings import wav_audio

from simple_chatbot.activity import Activity, ActivityObserver, ObservedSmartTurn
from simple_chatbot.diarization import SpeakerSpan
from simple_chatbot.runtime_info import configured_models, health_metadata
from simple_chatbot.services import HTTPSTT, HTTPTTS, GatewayLLM
from simple_chatbot.speakers import SpeakerProfiles
from simple_chatbot.timings import TurnTimings


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_discards_old_activity_and_resumes_with_fresh_input(self):
        activity = Activity()
        observer = ActivityObserver(activity)
        activity.set("diarization", "off", "Not configured")
        activity.begin("llm", "Old response")
        old_stt = activity.begin("stt", "Old utterance")
        activity.set("output", "active", "Sending speech audio")
        analyzer = ObservedSmartTurn(activity)
        entered, release = asyncio.Event(), asyncio.Event()

        async def predict():
            entered.set()
            await release.wait()
            return EndOfTurnState.COMPLETE, None

        try:
            with patch.object(LocalSmartTurnAnalyzerV3, "analyze_end_of_turn", side_effect=predict):
                task = asyncio.create_task(analyzer.analyze_end_of_turn())
                await asyncio.wait_for(entered.wait(), 1)
                activity.pause()
                for frame in (
                    InterruptionFrame(),
                    BotStartedSpeakingFrame(),
                    InputAudioRawFrame(bytes(640), 16000, 1),
                ):
                    await observer.on_push_frame(
                        FramePushed(None, None, frame, FrameDirection.DOWNSTREAM, 0)
                    )
                activity.end("stt", old_stt)
                self.assertTrue(activity.snapshot()["paused"])
                self.assertIsNone(activity.interruption)
                self.assertFalse(any(activity.operations.values()))
                self.assertTrue(all(state["status"] == "off" for state in activity.states.values()))
                self.assertEqual(activity.states["output"]["label"], "Conversation paused")
                activity.resume()
                fresh_stt = activity.begin("stt", "Fresh utterance")
                activity.end("stt", old_stt)
                release.set()
                await task
                self.assertFalse(activity.turn_complete)
                self.assertEqual(activity.states["stt"]["label"], "Fresh utterance")
                self.assertEqual(activity.states["turn"]["status"], "waiting")
                self.assertEqual(activity.states["diarization"]["label"], "Not configured")
                activity.end("stt", fresh_stt)
                self.assertEqual(activity.states["stt"]["status"], "complete")
                self.assertFalse(activity.snapshot()["paused"])
        finally:
            release.set()
            await analyzer.cleanup()

    async def test_real_streaming_pipeline_speaks_before_llm_finishes(self):
        activity = Activity()
        first_audio, release = asyncio.Event(), asyncio.Event()
        config = settings(Path("unused"))

        class Tokens(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse(
                    event("Hello there, this is a complete first sentence. Another "), done=False
                )
                await release.wait()
                yield sse(event("sentence."), event("", finish="stop"))

        class Collector(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, TTSAudioRawFrame):
                    first_audio.set()
                await self.push_frame(frame, direction)

        def respond(request):
            if request.url.path.endswith("chat/completions"):
                return httpx.Response(
                    200, stream=Tokens(), headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(200, content=bytes(4800), headers={"content-type": "audio/pcm"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            llm = GatewayLLM(
                config, http, "synthetic-key", lambda _: None, TurnTimings(AsyncMock(), activity)
            )
            worker = PipelineWorker(
                Pipeline([llm, HTTPTTS(config.tts, http, activity), Collector()]),
                enable_rtvi=False,
                enable_turn_tracking=False,
                idle_timeout_secs=None,
            )
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                await worker.queue_frame(LLMContextFrame(LLMContext([])))
                await asyncio.wait_for(first_audio.wait(), 3)
                self.assertEqual(activity.states["llm"]["status"], "active")
                self.assertTrue(
                    any(e["stage"] == "tts" and e["status"] == "active" for e in activity.events)
                )
                release.set()
                await worker.queue_frame(EndFrame())
                await asyncio.wait_for(running, 3)
                self.assertEqual(activity.states["llm"]["status"], "complete")
                self.assertEqual(activity.states["buffer"]["status"], "complete")
            finally:
                release.set()
                await runner.cancel()
                await running

    async def test_smart_turn_waits_for_continuation_and_transcript(self):
        activity = Activity()
        analyzer = ObservedSmartTurn(activity)
        entered, release = asyncio.Event(), asyncio.Event()

        async def predict():
            entered.set()
            await release.wait()
            return EndOfTurnState.INCOMPLETE, None

        try:
            with patch.object(LocalSmartTurnAnalyzerV3, "analyze_end_of_turn", side_effect=predict):
                task = asyncio.create_task(analyzer.analyze_end_of_turn())
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(activity.states["turn"]["status"], "active")
                release.set()
                await task
                self.assertEqual(activity.states["turn"]["status"], "waiting")
            with patch.object(
                LocalSmartTurnAnalyzerV3,
                "analyze_end_of_turn",
                return_value=(EndOfTurnState.COMPLETE, None),
            ):
                await analyzer.analyze_end_of_turn()
            self.assertEqual(activity.states["ready"]["status"], "waiting")
            activity.transcript_ready = True
            activity.gate()
            self.assertEqual(activity.states["ready"]["status"], "complete")
        finally:
            await analyzer.cleanup()

    async def test_concurrency_interruption_and_snapshot_isolation(self):
        a, other = Activity(), Activity()
        old = a.begin("tts", "First request")
        second = a.begin("tts", "Second request")
        saved = a.snapshot()
        a.end("tts", old)
        self.assertEqual(a.states["tts"]["status"], "active")
        a.interrupt()
        new = a.begin("tts", "New reply")
        a.end("tts", second)
        self.assertEqual(a.states["tts"]["label"], "New reply")
        self.assertEqual(other.states["tts"]["status"], "waiting")
        self.assertEqual(saved["states"]["tts"]["label"], "Second request")
        a.end("tts", new)
        for index in range(500):
            a.set("stt", "active" if index % 2 else "waiting", "Synthetic state")
        self.assertEqual(len(a.events), 160)
        self.assertEqual(sorted(e["at_ms"] for e in a.events), [e["at_ms"] for e in a.events])
        a.close()
        a.end("tts", second)
        self.assertNotEqual(a.states["tts"]["status"], "active")

    async def test_barge_in_detection_is_separate_from_server_output_stop(self):
        a = Activity()
        observer = ActivityObserver(a)

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            await observer.on_push_frame(FramePushed(None, None, frame, direction, 0))

        await push(InputAudioRawFrame(bytes(640), 16000, 1))
        await push(VADUserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.assertTrue(a.speaking)
        await push(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.assertFalse(a.speaking)
        a.begin("llm", "Streaming response")
        await push(BotStartedSpeakingFrame())
        await push(InterruptionFrame())
        self.assertEqual(a.states["llm"]["status"], "interrupted")
        self.assertEqual(a.states["output"]["status"], "active")
        self.assertNotIn("output_stopped_ms", a.interruption)
        saved = a.snapshot()
        await push(BotStoppedSpeakingFrame())
        self.assertEqual(a.states["output"]["status"], "interrupted")
        self.assertGreaterEqual(a.interruption["output_stopped_ms"], a.interruption["at_ms"])
        self.assertNotIn("output_stopped_ms", saved["interruption"])

    async def test_slow_ui_does_not_block_activity_or_create_a_send_backlog(self):
        a = Activity()
        sending = asyncio.Event()
        sent = []

        async def send(payload):
            sent.append(payload)
            sending.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(a.publish(send))
        await asyncio.wait_for(sending.wait(), 1)
        a.audio()
        for _ in range(200):
            with a.work("stt", "Transcribing segment"):
                pass
        self.assertEqual(a.states["input"]["status"], "active")
        self.assertEqual(len(sent), 1)
        self.assertLessEqual(len(a.events), 160)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_llm_activity_remains_active_until_stream_finishes_and_cancels(self):
        a = Activity()
        text_sent = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse(event("Hello."), done=False)
                text_sent.set()
                await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=Stream(), headers={"content-type": "text/event-stream"}
                )
            )
        ) as http:
            llm = GatewayLLM(
                settings(Path("unused")),
                http,
                "synthetic-key",
                lambda _: None,
                TurnTimings(AsyncMock(), a),
            )
            llm.push_frame = AsyncMock()
            task = asyncio.create_task(
                llm.process_frame(LLMContextFrame(LLMContext([])), FrameDirection.DOWNSTREAM)
            )
            await asyncio.wait_for(text_sent.wait(), 1)
            with a.work("tts", "Receiving speech audio"):
                self.assertEqual(a.states["llm"]["status"], "active")
                self.assertEqual(a.states["tts"]["status"], "active")
                a.interrupt()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(a.states["llm"]["status"], "interrupted")
            self.assertEqual(a.states["tts"]["status"], "interrupted")
            self.assertNotIn("synthetic-key", json.dumps(a.snapshot()))


class RuntimeInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_credential_free_bounded_and_does_not_follow_redirects(self):
        requests = []
        responses = iter(
            [
                httpx.Response(200, json={"device": "cuda", "secret": "must-not-appear"}),
                httpx.Response(302, headers={"location": "https://other.example.com/private"}),
                httpx.Response(200, stream=Chunks([b" " * 16385, b'{"device":"cpu"}'])),
                httpx.Response(200, json={"device": ["cuda"]}),
                httpx.Response(200, content=b"[" * 2000),
            ]
        )

        def respond(request):
            requests.append(request)
            self.assertEqual(str(request.url), "http://192.168.50.7:8010/health")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            return next(responses)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            headers={"Authorization": "secret"},
            auth=("user", "secret"),
            cookies={"private": "secret"},
        ) as http:
            url = "ws://user:password@192.168.50.7:8010/v1/stream?token=secret"
            result = await health_metadata(http, url)
            self.assertEqual(result["compute"], "GPU")
            self.assertEqual(set(result), {"compute", "evidence", "checked_at"})
            for _ in range(4):
                self.assertEqual(await health_metadata(http, url), {})
        self.assertEqual(len(requests), 5)

    async def test_configured_route_is_not_a_claim_about_model_hardware(self):
        config = settings(Path("unused"))
        config.model_id = "remote/example/vendor/model"
        info = configured_models(config)
        self.assertEqual(info["llm"]["host"], "example")
        self.assertEqual(info["llm"]["via"], "gateway.example.com")
        self.assertEqual(info["llm"]["compute"], "Unknown")
        self.assertEqual(info["stt"]["hosting"], "Remote endpoint")


class SpeakerTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_completion_and_per_turn_totals(self):
        now = 100.0
        clock = SimpleNamespace(monotonic=lambda: now)
        events = []
        futures = []

        def respond(_):
            nonlocal now
            now += 0.5
            return httpx.Response(200, json={"text": "Hello"})

        async def send(payload):
            events.append(payload)
            if payload["status"] == "transcribed" and futures and not futures[-1].done():

                def complete():
                    nonlocal now
                    now += 0.25
                    futures[-1].set_result(SpeakerSpan(0, 16000, 0, (), 0.75))

                asyncio.get_running_loop().call_soon(complete)

        timings = TurnTimings(send)
        diarization = SimpleNamespace(
            profiles=SpeakerProfiles(), settings=SimpleNamespace(timeout_seconds=1)
        )
        with (
            patch("simple_chatbot.services.time", clock),
            patch("simple_chatbot.timings.time", clock),
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                stt = HTTPSTT(settings(Path("unused")).stt, http, timings, diarization)
                for ready in (True, False):
                    future = asyncio.get_running_loop().create_future()
                    futures.append(future)
                    if ready:
                        future.set_result(SpeakerSpan(0, 16000, 0, (), 0.125))
                    stt._speaker_segments.append(future)
                    _ = [frame async for frame in stt.run_stt(wav_audio(1))]
                turn = await timings.begin_llm()
        self.assertEqual(turn.values["stt_ms"], 1000)
        self.assertEqual(turn.values["diarization_finalize_ms"], 875)
        self.assertEqual(turn.values["diarization_wait_ms"], 250)
        self.assertEqual((await timings.begin_llm()).values, {})

    async def test_unresolved_speaker_result_never_publishes_success_after_failure(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                events = []
                transcribed = asyncio.Event()

                async def send(payload, events=events, transcribed=transcribed):
                    events.append(payload)
                    if payload["status"] == "transcribed":
                        transcribed.set()

                timings = TurnTimings(send)
                future = asyncio.get_running_loop().create_future()
                diarization = SimpleNamespace(
                    profiles=SpeakerProfiles(), settings=SimpleNamespace(timeout_seconds=1)
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json={"text": "Hi"})
                    )
                ) as http:
                    stt = HTTPSTT(settings(Path("unused")).stt, http, timings, diarization)
                    stt.push_error = AsyncMock()
                    stt._speaker_segments.append(future)

                    async def consume(stt=stt):
                        return [frame async for frame in stt.run_stt(wav_audio(1))]

                    task = asyncio.create_task(consume())
                    await asyncio.wait_for(transcribed.wait(), 1)
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        future.set_exception(TimeoutError())
                        await task
                    saved = json.dumps(events)
                    if not future.done():
                        future.set_result(SpeakerSpan(0, 16000, 0, (), 1))
                    self.assertEqual(json.dumps(events), saved)
                    self.assertIn("stt_ms", events[-1])
                    self.assertNotIn("diarization_finalize_ms", events[-1])
                    self.assertNotIn("diarization_wait_ms", events[-1])
                    self.assertEqual(events[-1]["status"], "interrupted" if cancel else "failed")
