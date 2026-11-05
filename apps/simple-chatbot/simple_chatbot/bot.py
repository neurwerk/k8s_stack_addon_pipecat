# Copyright (c) 2024–2025, Daily
# SPDX-License-Identifier: BSD-2-Clause
# Adapted from pipecat-ai/pipecat-examples simple-chatbot/server/bot-openai.py.
# See UPSTREAM-LICENSE and upstream.json for source attribution.

"""App-owned voice pipeline: independent context and processors per connection."""

import asyncio
from collections.abc import Callable

import httpx
from fastapi import WebSocket
from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import (
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

from simple_chatbot.config import ConversationOptions, Settings
from simple_chatbot.diarization import DiarizationSession
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
    if diarization:
        await diarization.attach()
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
                "content": (
                    "You are Chatbot, a friendly, helpful robot. "
                    "Demonstrate your capabilities succinctly. "
                    "Your output is spoken aloud; avoid special formatting. "
                    "Keep your answers brief. "
                    "Start by introducing yourself."
                    + (
                        " If speaker names are saved, greet the group without assigning a "
                        "voice until someone speaks. Otherwise ask one person to introduce "
                        "themselves with their name and a short sentence, then ask if anybody "
                        "else is joining. Introductions are spoken, without buttons."
                        if diarization
                        else ""
                    )
                ),
            }
        ]
    )
    user, assistant = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
            ),
        ),
    )

    async def send_timing(data: dict) -> None:
        await worker.rtvi.send_server_message(data)

    timings = TurnTimings(send_timing)

    class PauseGate(FrameProcessor):
        paused = False
        llm_boundary = False

        async def process_frame(self, frame, direction: FrameDirection):
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
            HTTPTTS(settings.tts, http),
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
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    greeted = False
    control_lock = asyncio.Lock()
    last_sequence = -1
    control_failed = False

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
                        vad._vad_state = VADState.QUIET
                        vad._vad_analyzer._vad_state = VADState.QUIET
                        vad._vad_analyzer._model.reset_states()
                    await controller.start()
                    if timings.pending:
                        await timings.pending.finish("interrupted")
                        timings.pending = None
                elif data["action"] == "resume" and gate.paused:
                    stt.resume_input()
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
