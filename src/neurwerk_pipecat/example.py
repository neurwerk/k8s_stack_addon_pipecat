# Copyright (c) 2024–2025, Daily
# SPDX-License-Identifier: BSD-2-Clause
# Adapted from pipecat-ai/pipecat-examples simple-chatbot/server/bot-openai.py.
# See docs/source.md and LICENSES/pipecat-examples.txt for the exact source and license.

"""Bundled public example: independent context and processors for each connection."""

from collections.abc import Callable

import httpx
from fastapi import WebSocket
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
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

from neurwerk_pipecat.config import Settings
from neurwerk_pipecat.services import HTTPSTT, HTTPTTS, GatewayLLM
from neurwerk_pipecat.websocket import BrowserSerializer


async def run_bot(
    connection: SmallWebRTCConnection | WebSocket,
    settings: Settings,
    http: httpx.AsyncClient,
    credential: str,
    notice: Callable[[str], None],
) -> None:
    assert settings.stt and settings.tts
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
    pipeline = Pipeline(
        [
            transport.input(),
            HTTPSTT(settings.stt, http),
            user,
            GatewayLLM(settings, http, credential, notice),
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

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        nonlocal greeted
        if greeted:
            return
        greeted = True
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        await runner.cancel()

    try:
        await runner.run()
    finally:
        await runner.cancel()
