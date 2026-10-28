# Copyright (c) 2024–2025, Daily
# SPDX-License-Identifier: BSD-2-Clause
# Uses the shared adaptation of pipecat-ai/pipecat-examples:
# simple-chatbot/server/bot-openai.py at 6ede04385ac2cf1d3a4cd0863a5dd33969ceccae.
# See LICENSE.txt for the upstream license.

"""S3-loaded sample using the runtime's authenticated, PII-aware voice pipeline."""

from collections.abc import Callable

import httpx
from fastapi import WebSocket
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

from neurwerk_pipecat.config import Settings
from neurwerk_pipecat.example import run_bot as run_example_bot


async def run_bot(
    connection: SmallWebRTCConnection | WebSocket,
    settings: Settings,
    http: httpx.AsyncClient,
    credential: str,
    notice: Callable[[str], None],
) -> None:
    await run_example_bot(connection, settings, http, credential, notice)
