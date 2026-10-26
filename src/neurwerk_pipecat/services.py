"""Bounded HTTP services. Only gateway content deltas can become speech."""

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable

import httpx
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

from neurwerk_pipecat.config import Settings, Speech
from neurwerk_pipecat.notices import NoticeSplitter, SpeechControlFilter


def speech_headers(speech: Speech) -> dict[str, str]:
    return {"Authorization": "Bearer " + speech.key.read()} if speech.key else {}


async def bounded_sse(response: httpx.Response) -> AsyncIterator[dict]:
    """OpenAI's one-data-line SSE profile; reject oversized/multiline events."""
    pending = b""
    total = 0
    async for chunk in response.aiter_bytes(chunk_size=4096):
        total += len(chunk)
        if total > 2_000_000:
            raise ValueError("Response limit exceeded")
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if len(line) > 65536:
                raise ValueError("Event limit exceeded")
            if line.startswith(b"data:"):
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                event = json.loads(data)
                if not isinstance(event, dict):
                    raise ValueError("Invalid event")
                yield event
        if len(pending) > 65536:
            raise ValueError("Event limit exceeded")
    raise ValueError("Incomplete gateway stream")


class GatewayLLM(FrameProcessor):
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        credential: str,
        notice: Callable[[str], None],
    ):
        super().__init__()
        self.settings = settings
        self.http = http
        self.credential = credential
        self.notice = notice

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        await self.push_frame(LLMFullResponseStartFrame())
        splitter = NoticeSplitter()
        speech = SpeechControlFilter()
        try:
            messages = frame.context.get_messages()
            if len(json.dumps(messages)) > self.settings.max_history_chars:
                raise ValueError("Conversation limit reached")
            async with self.http.stream(
                "POST",
                self.settings.gateway_url,
                headers={"X-Neurwerk-Authorization": "Bearer " + self.credential},
                json={
                    "model": self.settings.model_id,
                    "messages": messages,
                    "stream": True,
                    "max_tokens": 512,
                },
            ) as response:
                response.raise_for_status()
                content_size = 0
                async for event in bounded_sse(response):
                    if "error" in event:
                        raise ValueError("Gateway rejected request")
                    for choice in event.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta", {})
                        # Reasoning, tool calls, function calls and metadata are never speech.
                        text = delta.get("content")
                        if isinstance(text, str):
                            content_size += len(text)
                            if content_size > 65536:
                                raise ValueError("Content limit exceeded")
                            clean = speech.feed(splitter.feed(text))
                            if clean:
                                await self.push_frame(LLMTextFrame(clean))
            splitter.finish()
            if splitter.notice:
                self.notice(splitter.notice)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # No upstream payloads or credentials in errors or logs.
            await self.push_error(
                "Model request failed or conversation limit reached", force_treat_as_permanent=True
            )
        finally:
            await self.push_frame(LLMFullResponseEndFrame())


class HTTPSTT(SegmentedSTTService):
    def __init__(self, settings: Speech, http: httpx.AsyncClient):
        super().__init__(sample_rate=16000)
        self.speech = settings
        self.http = http

    async def process_audio_frame(self, frame: InputAudioRawFrame, direction: FrameDirection):
        # Pinned Pipecat's segmented adapter has an unbounded queue and utterance buffer.
        # Fail closed rather than silently truncating a user's words.
        if len(self._audio_buffer) > 16000 * 2 * 30 or self._segment_queue.qsize() >= 4:
            self._audio_buffer.clear()
            await self.push_error("Speech input limit reached", force_treat_as_permanent=True)
            return
        await super().process_audio_frame(frame, direction)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        try:
            async with self.http.stream(
                "POST",
                self.speech.base_url.rstrip("/") + "/audio/transcriptions",
                headers=speech_headers(self.speech),
                files={"file": ("speech.wav", audio, "audio/wav")},
                data={
                    "model": self.speech.model,
                    "language": self.speech.language,
                    "response_format": "json",
                },
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise ValueError("Transcript limit reached")
                text = json.loads(data)["text"]
                if not isinstance(text, str) or len(text) > 8192:
                    raise ValueError("Invalid transcript")
                yield TranscriptionFrame(text, "", time_now_iso8601())
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            await self.push_error("Speech recognition failed", force_treat_as_permanent=True)


class HTTPTTS(TTSService):
    def __init__(self, settings: Speech, http: httpx.AsyncClient):
        super().__init__(sample_rate=24000, push_start_frame=True, push_stop_frames=True)
        self.speech = settings
        self.http = http

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        try:
            async with self.http.stream(
                "POST",
                self.speech.base_url.rstrip("/") + "/audio/speech",
                headers=speech_headers(self.speech),
                json={
                    "model": self.speech.model,
                    "voice": self.speech.voice,
                    "input": text,
                    "response_format": "pcm",
                },
            ) as response:
                response.raise_for_status()
                # Endpoint must stream raw signed little-endian PCM16, 24 kHz mono.
                pending = b""
                total = 0
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    total += len(chunk)
                    if total > 24000 * 2 * 120:
                        raise ValueError("Audio limit reached")
                    pending += chunk
                    size = len(pending) // 2 * 2
                    if size:
                        yield TTSAudioRawFrame(pending[:size], 24000, 1, context_id=context_id)
                        pending = pending[size:]
                if pending:
                    raise ValueError("Invalid PCM sample")
        except (httpx.HTTPError, ValueError):
            await self.push_error("Speech synthesis failed", force_treat_as_permanent=True)
