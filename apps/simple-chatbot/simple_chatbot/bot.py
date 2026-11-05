# Copyright (c) 2024–2025, Daily
# SPDX-License-Identifier: BSD-2-Clause
# Adapted from pipecat-ai/pipecat-examples simple-chatbot/server/bot-openai.py.
# See UPSTREAM-LICENSE and upstream.json for source attribution.

"""App-owned voice pipeline: independent context and processors per connection."""

import asyncio
import contextlib
from collections.abc import Callable

import httpx
from fastapi import WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMRunFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from simple_chatbot.activity import Activity, ActivityObserver, ObservedSmartTurn
from simple_chatbot.config import ConversationOptions, Settings
from simple_chatbot.diarization import DiarizationSession
from simple_chatbot.prompts import initial_prompt
from simple_chatbot.runtime_info import configured_models, onnx_session_metadata, refresh_models
from simple_chatbot.services import HTTPSTT, HTTPTTS, GatewayLLM
from simple_chatbot.timings import TurnTimings
from simple_chatbot.websocket import BrowserSerializer


async def run_bot(
    connection: SmallWebRTCConnection | WebSocket,
    settings: Settings,
    http: httpx.AsyncClient,
    credential: str,
    notice: Callable[[str], None],
    options: ConversationOptions | None = None,
    diarization: DiarizationSession | None = None,
) -> None:
    assert settings.stt and settings.tts
    activity = Activity()
    activity.models = configured_models(settings)
    if diarization:
        await diarization.attach()
        diarization.activity = activity
    else:
        activity.set("diarization", "off", "Not configured")
        activity.set("attribution", "off", "Not configured")
    transport: BaseTransport
    if isinstance(connection, WebSocket):
        transport = FastAPIWebsocketTransport(
            websocket=connection,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                serializer=BrowserSerializer(),
                session_timeout=settings.session_seconds,
            ),
        )
    else:
        transport = SmallWebRTCTransport(
            connection, TransportParams(audio_in_enabled=True, audio_out_enabled=True)
        )
    context = LLMContext(
        messages=[
            {
                "role": "system",
                "content": initial_prompt(diarization is not None),
            }
        ]
    )
    vad_analyzer = SileroVADAnalyzer()
    turn_analyzer = ObservedSmartTurn(activity)
    local_models = {
        "vad": {
            **activity.models["vad"],
            **onnx_session_metadata(vad_analyzer._model.session.get_providers()),
        },
        "turn": {
            **activity.models["turn"],
            **onnx_session_metadata(turn_analyzer._session.get_providers()),
        },
    }
    activity.models.update(local_models)
    user, assistant = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer,
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn_analyzer)]
            ),
        ),
    )

    async def send_timing(data: dict) -> None:
        await worker.rtvi.send_server_message(data)

    timings = TurnTimings(send_timing, activity)

    class PauseGate(FrameProcessor):
        paused = False
        llm_boundary = False

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            input_frames = (
                InputAudioRawFrame,
                LLMRunFrame,
                VADUserStartedSpeakingFrame,
                VADUserStoppedSpeakingFrame,
            )
            if (
                self.paused
                and direction == FrameDirection.DOWNSTREAM
                and isinstance(
                    frame, (LLMContextFrame, LLMRunFrame) if self.llm_boundary else input_frames
                )
            ):
                return
            await self.push_frame(frame, direction)

    gate = PauseGate()
    llm_gate = PauseGate()
    llm_gate.llm_boundary = True
    stt = HTTPSTT(settings.stt, http, timings, diarization=diarization)
    pipeline = Pipeline(
        [
            transport.input(),
            gate,
            stt,
            user,
            llm_gate,
            GatewayLLM(
                settings,
                http,
                credential,
                notice,
                timings,
                options,
                profiles=diarization.profiles if diarization else None,
                forget_speakers=diarization.reset if diarization else None,
            ),
            HTTPTTS(settings.tts, http, activity),
            transport.output(),
            assistant,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            enable_metrics=False,
            enable_usage_metrics=False,
        ),
        idle_timeout_secs=settings.idle_seconds,
        processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
        observers=[ActivityObserver(activity)],
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    greeted = False
    control_lock = asyncio.Lock()
    last_sequence = -1
    control_failed = False
    activity_tasks: list[asyncio.Task] = []

    @user.event_handler("on_user_turn_stopped")
    async def on_turn_stopped(aggregator, strategy, message):
        if activity.paused or activity.closed:
            return
        activity.turn_complete = True
        activity.set("turn", "complete", "Turn ended" if strategy else "Turn timeout")
        activity.gate()

    # The pinned aggregator dispatches handlers in detached tasks by default.
    # Run this content-free status update inline with the turn frame so the
    # pause flush cannot acknowledge before an older turn callback executes.
    user._event_handlers["on_user_turn_stopped"].is_sync = True

    @worker.event_handler("on_pipeline_error")
    async def on_pipeline_error(worker, frame):
        logger.warning(
            "Voice pipeline error processor={} category={} usable={}",
            type(frame.processor).__name__,
            frame.category.value if frame.category else "unknown",
            frame.processor.is_usable if frame.processor else None,
        )

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        nonlocal greeted
        if greeted:
            return
        greeted = True
        activity_tasks.extend(
            [
                asyncio.create_task(activity.publish(send_timing)),
                asyncio.create_task(refresh_models(activity, settings, local_models)),
            ]
        )
        await worker.queue_frames([LLMRunFrame()])

    @worker.rtvi.event_handler("on_client_message")
    async def on_control(rtvi, message):
        nonlocal last_sequence, control_failed
        data = message.data
        if (
            message.type != "conversation-control"
            or not isinstance(data, dict)
            or set(data) != {"action", "sequence"}
            or data["action"] not in ("pause", "resume")
            or type(data["sequence"]) is not int
            or not 0 <= data["sequence"] <= 1000000
        ):
            return
        async with control_lock:
            if control_failed or data["sequence"] <= last_sequence:
                return
            last_sequence = data["sequence"]
            try:
                if data["action"] == "pause" and not gate.paused:
                    gate.paused = llm_gate.paused = True
                    activity.pause()
                    await stt.pause_input()
                    await rtvi.interrupt_bot()
                    # The pinned user aggregator keeps its turn/SmartTurn/VAD state
                    # across interruptions. Discard only the partial utterance;
                    # its LLM context (previous turns) and speaker profiles survive.
                    controller = user._user_turn_controller
                    await controller.stop()
                    controller._user_speaking = False
                    controller._user_turn = False
                    for strategy in controller.user_turn_strategies.stop or []:
                        await strategy.handle_user_turn_stopped()
                        if isinstance(strategy, TurnAnalyzerUserTurnStopStrategy):
                            strategy._vad_user_speaking = False
                    await user.reset()
                    user._full_user_turn_aggregation = None
                    user._user_turn_interrupted_bot = False
                    user._user_turn_start_timestamp = ""
                    vad = user._vad_controller
                    if vad:
                        # VAD runs in a single-thread executor. Enqueue the reset
                        # there so an in-flight analysis cannot restore old speech
                        # after the partial audio and turn have been discarded.
                        def reset_vad() -> None:
                            analyzer = vad._vad_analyzer
                            analyzer._vad_buffer = b""
                            analyzer._vad_state = VADState.QUIET
                            analyzer._vad_starting_count = 0
                            analyzer._vad_stopping_count = 0
                            analyzer._prev_volume = 0.0
                            analyzer._volume_tracker = type(analyzer._volume_tracker)()
                            analyzer._model.reset_states()
                            analyzer._windows_since_reset = 0
                            analyzer._low_confidence_windows = 0

                        await asyncio.wait_for(
                            asyncio.get_running_loop().run_in_executor(
                                vad_analyzer._executor, reset_vad
                            ),
                            timeout=3,
                        )
                        vad._vad_state = VADState.QUIET
                    await controller.start()
                    if timings.pending:
                        await timings.pending.finish("interrupted")
                        timings.pending = None
                    # interrupt_bot only enqueues interruption frames. Wait for
                    # the pinned worker's downstream/upstream flush before ack.
                    async with asyncio.timeout(4):
                        if not await worker.flush_pipeline(timeout=2):
                            raise RuntimeError("Pipeline did not stop")
                elif data["action"] == "resume" and gate.paused:
                    stt.resume_input()
                    activity.resume()
                    gate.paused = llm_gate.paused = False
                await rtvi.send_server_message(
                    {
                        "type": "conversation-control",
                        "paused": gate.paused,
                        "sequence": data["sequence"],
                    }
                )
            except Exception:
                control_failed = True
                gate.paused = llm_gate.paused = True
                await stt.pause_input()
                raise

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        await runner.cancel()

    try:
        await runner.run()
    finally:
        await runner.cancel()
        activity.close()
        for task in activity_tasks:
            task.cancel()
        for task in activity_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if diarization:
            diarization.activity = None
