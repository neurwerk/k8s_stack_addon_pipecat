"""Bounded HTTP services. Only gateway content deltas can become speech."""

import codecs
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

from simple_chatbot.config import Settings, Speech
from simple_chatbot.notices import NoticeSplitter, SpeechControlFilter


def speech_headers(speech: Speech) -> dict[str, str]:
    return {"Authorization": "Bearer " + speech.key.read()} if speech.key else {}


async def bounded_sse(response: httpx.Response) -> AsyncIterator[dict]:
    """Incremental SSE with a strict single-choice, completed chat response boundary."""
    if (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "text/event-stream"
    ):
        raise ValueError("Expected an event stream")
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    data_lines: list[str] = []
    event_size = 0
    total = 0
    saw_choice = saw_content = finished = False
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > 2_000_000:
            raise ValueError("Response limit exceeded")
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.removesuffix("\r")
            if len(line.encode()) > 65536:
                raise ValueError("Event limit exceeded")
            if not line and data_lines:
                data = "\n".join(data_lines)
                data_lines, event_size = [], 0
                if data == "[DONE]":
                    if not (saw_choice and saw_content and finished):
                        raise ValueError("Incomplete gateway completion")
                    return
                event = json.loads(data)
                if not isinstance(event, dict) or "error" in event:
                    raise ValueError("Invalid event")
                choices = event.get("choices")
                if not isinstance(choices, list):
                    raise ValueError("Missing choices")
                if not choices:
                    if not isinstance(event.get("usage"), dict):
                        raise ValueError("Only usage events may have empty choices")
                    yield event
                    continue
                if len(choices) != 1 or not isinstance(choices[0], dict):
                    raise ValueError("Invalid choices")
                choice = choices[0]
                delta = choice.get("delta")
                if (
                    type(choice.get("index")) is not int
                    or choice["index"] != 0
                    or not isinstance(delta, dict)
                    or finished
                ):
                    raise ValueError("Invalid selected choice")
                if delta.get("role") not in (None, "assistant"):
                    raise ValueError("Invalid assistant role")
                for field in ("content", "reasoning_content", "reasoning"):
                    if delta.get(field) is not None and not isinstance(delta[field], str):
                        raise ValueError("Invalid text delta")
                if delta.get("tool_calls") is not None and not isinstance(
                    delta["tool_calls"], list
                ):
                    raise ValueError("Invalid tool delta")
                saw_choice = True
                saw_content |= bool((delta.get("content") or "").strip())
                reason = choice.get("finish_reason")
                if reason is not None:
                    if reason != "stop":
                        raise ValueError("Unsuccessful gateway completion")
                    finished = True
                yield event
            elif line.startswith("data:") or line == "data":
                value = line.partition(":")[2].removeprefix(" ")
                event_size += len(value.encode()) + 1
                if event_size > 65536:
                    raise ValueError("Event limit exceeded")
                data_lines.append(value)
        if len(pending.encode()) > 65536:
            raise ValueError("Event limit exceeded")
    decoder.decode(b"", final=True)
    raise ValueError("Incomplete gateway stream")


async def bounded_pcm(response: httpx.Response) -> AsyncIterator[bytes]:
    """Validate raw PCM media type and sniff a bounded prefix before emitting any audio."""
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media not in {"audio/pcm", "audio/x-pcm", "application/octet-stream"}:
        raise ValueError("Expected raw PCM audio")
    parameters = dict(
        part.strip().lower().partition("=")[::2]
        for part in response.headers.get("content-type", "").split(";")[1:]
    )
    for key, expected in (
        ("rate", "24000"),
        ("samplerate", "24000"),
        ("channels", "1"),
        ("bits", "16"),
        ("bitdepth", "16"),
    ):
        if key in parameters and parameters[key].strip('"') != expected:
            raise ValueError("Unsupported PCM format")
    prefix = b""
    pending = b""
    total = 0
    checked = False

    def check(data: bytes) -> None:
        signatures = (
            b"RIFF",
            b"RIFX",
            b"RF64",
            b"FORM",
            b"OggS",
            b"fLaC",
            b"ID3",
            b"\x1a\x45\xdf\xa3",
            b"PK\x03\x04",
            b"\x00\x00\x01\xba",
        )
        text = data.removeprefix(b"\xef\xbb\xbf").lstrip()
        if (
            any(data.startswith(signature) for signature in signatures)
            or data[4:8] == b"ftyp"
            or text.startswith((b"{", b"[", b"<", b'"'))
            or text.lower().startswith((b"error", b"unauthorized", b"forbidden"))
            or (len(data) >= 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0)
        ):
            raise ValueError("Encoded audio or error payload is not raw PCM")

    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > 24000 * 2 * 120:
            raise ValueError("Audio limit reached")
        pending += chunk
        if not checked:
            prefix = pending[:32]
            if len(prefix) < 32:
                continue
            check(prefix)
            checked = True
        size = len(pending) // 2 * 2
        for offset in range(0, size, 4096):
            yield pending[offset : min(offset + 4096, size)]
        pending = pending[size:]
    if not total or len(pending) % 2:
        raise ValueError("Empty or incomplete PCM sample")
    if not checked:
        check(prefix if prefix else pending[:32])
        if pending:
            yield pending


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
        spoken = False
        try:
            messages = frame.context.get_messages()
            if len(json.dumps(messages)) > self.settings.max_history_chars:
                raise ValueError("Conversation limit reached")
            async with self.http.stream(
                "POST",
                self.settings.gateway_url,
                follow_redirects=False,
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
                                spoken |= bool(clean.strip())
                                await self.push_frame(LLMTextFrame(clean))
            if not spoken:
                raise ValueError("No assistant speech content")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # No upstream payloads or credentials in errors or logs.
            await self.push_error(
                "Model request failed or conversation limit reached", force_treat_as_permanent=True
            )
        finally:
            splitter.finish()
            if splitter.notice:
                self.notice(splitter.notice)
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
                async for audio in bounded_pcm(response):
                    yield TTSAudioRawFrame(audio, 24000, 1, context_id=context_id)
        except (httpx.HTTPError, ValueError):
            await self.push_error("Speech synthesis failed", force_treat_as_permanent=True)
