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
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
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
    pipeline = Pipeline(
        [
            transport.input(),
            HTTPSTT(settings.stt, http, timings, diarization=diarization),
            user,
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
    activity_tasks: list[asyncio.Task] = []

    @user.event_handler("on_user_turn_stopped")
    async def on_turn_stopped(aggregator, strategy, message):
        activity.turn_complete = True
        activity.set("turn", "complete", "Turn ended" if strategy else "Turn timeout")
        activity.gate()

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
