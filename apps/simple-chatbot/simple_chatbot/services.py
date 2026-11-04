"""Bounded HTTP services. Only gateway content deltas can become speech."""

import asyncio
import codecs
import io
import json
import time
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Callable

import httpx
from loguru import logger
from pipecat.frames.frames import (
    AggregatedTextFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
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
from pipecat.utils.errors import ErrorCategory, classify_http_status_code
from pipecat.utils.text.transforms import strip_markdown
from pipecat.utils.time import time_now_iso8601

from simple_chatbot.config import ConversationOptions, Settings, Speech
from simple_chatbot.notices import NoticeSplitter, SpeechControlFilter
from simple_chatbot.timings import TurnTiming, TurnTimings


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
                ):
                    raise ValueError("Invalid selected choice")
                if finished:
                    # OpenRouter repeats the successful terminal choice in its
                    # final usage chunk. It is accounting, not another response.
                    if (
                        not isinstance(event.get("usage"), dict)
                        or choice.get("finish_reason") != "stop"
                        or delta.get("role") not in (None, "assistant")
                        or any(
                            key != "role" and value not in (None, "")
                            for key, value in delta.items()
                        )
                    ):
                        raise ValueError("Unexpected content after completion")
                    yield event
                    continue
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
        timings: TurnTimings | None = None,
        options: ConversationOptions | None = None,
    ):
        super().__init__()
        self.settings = settings
        self.http = http
        self.credential = credential
        self.notice = notice
        self.timings = timings
        self.options = options or ConversationOptions()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        timing = await self.timings.begin_llm() if self.timings else None
        start = LLMFullResponseStartFrame()
        if timing:
            start.metadata["turn_timing"] = timing
        await self.push_frame(start)
        try:
            messages = frame.context.get_messages()
            if len(json.dumps(messages)) > self.settings.max_history_chars:
                if timing:
                    await timing.finish("failed")
                await self.push_error(
                    "Conversation limit reached. Start a new conversation.",
                    force_treat_as_permanent=True,
                )
                return
            for attempt in range(2):
                splitter = NoticeSplitter()
                speech = SpeechControlFilter()
                spoken = emitted = retrying = False
                try:
                    if timing:
                        timing.llm_started = time.monotonic()
                    async with self.http.stream(
                        "POST",
                        self.settings.gateway_url,
                        follow_redirects=False,
                        headers={"Authorization": "Bearer " + self.credential},
                        json={
                            "model": self.settings.model_id,
                            "messages": messages,
                            "stream": True,
                            "max_tokens": 512,
                            **(
                                {"reasoning": {"effort": self.options.reasoning_effort}}
                                if self.options.reasoning_effort is not None
                                else {}
                            ),
                        },
                    ) as response:
                        response.raise_for_status()
                        content_size = 0
                        async for event in bounded_sse(response):
                            for choice in event.get("choices", []):
                                delta = choice.get("delta", {})
                                # Reasoning, tool calls and metadata are never speech.
                                text = delta.get("content")
                                if isinstance(text, str):
                                    content_size += len(text)
                                    if content_size > 65536:
                                        raise ValueError("Content limit exceeded")
                                    clean = speech.feed(splitter.feed(text))
                                    if clean:
                                        if timing and clean.strip():
                                            await timing.first_text()
                                        spoken |= bool(clean.strip())
                                        emitted = True
                                        await self.push_frame(LLMTextFrame(clean))
                    if not spoken:
                        raise ValueError("No assistant speech content")
                    break
                except httpx.HTTPStatusError as error:
                    if timing:
                        await timing.finish("failed")
                    await self.push_error(
                        f"Model request rejected (HTTP {error.response.status_code}).",
                        force_treat_as_permanent=True,
                    )
                    break
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                    if attempt == 0 and not emitted:
                        retrying = True
                        continue
                    if timing:
                        await timing.finish("failed")
                    # Exception details can contain upstream payloads or credentials.
                    await self.push_error(
                        "Model connection failed or timed out. Please retry."
                        if isinstance(error, httpx.HTTPError)
                        else "Model response was invalid or incomplete. Please retry.",
                        category=(
                            ErrorCategory.CONNECTIVITY
                            if isinstance(error, httpx.HTTPError)
                            else ErrorCategory.APPLICATION
                        ),
                    )
                    break
                finally:
                    splitter.finish()
                    if splitter.notice and not retrying:
                        self.notice(splitter.notice)
        except (ValueError, KeyError, TypeError):
            if timing:
                await timing.finish("failed")
            # Exception details can contain upstream payloads or credentials.
            await self.push_error(
                "Model response was invalid or incomplete. Please retry.",
                force_treat_as_permanent=True,
            )
        except asyncio.CancelledError:
            if timing:
                await timing.finish("interrupted")
            raise
        finally:
            await self.push_frame(LLMFullResponseEndFrame())


class HTTPSTT(SegmentedSTTService):
    def __init__(
        self, settings: Speech, http: httpx.AsyncClient, timings: TurnTimings | None = None
    ):
        super().__init__(sample_rate=16000)
        self.speech = settings
        self.http = http
        self.timings = timings

    async def process_audio_frame(self, frame: InputAudioRawFrame, direction: FrameDirection):
        # Pinned Pipecat's segmented adapter has an unbounded queue and utterance buffer.
        # Fail closed rather than silently truncating a user's words.
        if len(self._audio_buffer) > 16000 * 2 * 30 or self._segment_queue.qsize() >= 4:
            self._audio_buffer.clear()
            await self.push_error("Speech input limit reached", force_treat_as_permanent=True)
            return
        await super().process_audio_frame(frame, direction)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        timing = await self.timings.begin_stt() if self.timings else None
        audio_seconds = None
        if timing:
            try:
                with wave.open(io.BytesIO(audio)) as wav:
                    audio_seconds = wav.getnframes() / wav.getframerate()
            except (wave.Error, EOFError):
                pass
        started = time.monotonic()
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
                if timing:
                    await timing.transcribed(
                        time.monotonic() - started, len(text.split()), audio_seconds
                    )
                yield TranscriptionFrame(text, "", time_now_iso8601())
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            if timing:
                await timing.finish("failed")
            await self.push_error("Speech recognition failed", force_treat_as_permanent=True)
        except asyncio.CancelledError:
            if timing:
                await timing.finish("interrupted")
            raise


async def speech_text(text: str, text_type: str) -> str:
    text = await strip_markdown(text, text_type)
    # Sentence aggregation can leave isolated emphasis or fence markers. Kokoro
    # produces no phonemes for these; let TTSService skip the empty result.
    if all(char.isspace() or char in "*_`~-" for char in text):
        return ""
    return text


class HTTPTTS(TTSService):
    def __init__(self, settings: Speech, http: httpx.AsyncClient):
        super().__init__(
            sample_rate=24000,
            push_start_frame=True,
            push_stop_frames=True,
            # HTTP reads time out after 45 seconds. Do not expire a context
            # while a legitimate synthesis request is still waiting for audio.
            stop_frame_timeout_s=50,
            # HTTP failures are classified below; silence is not proof that
            # every future request will fail.
            max_consecutive_zero_audio_contexts=0,
            text_transforms=[("*", speech_text)],
        )
        self.speech = settings
        self.http = http
        self.timing: TurnTiming | None = None
        self._synthesis_failed = False

    async def tts_process_generator(self, context_id: str, generator):
        try:
            await super().tts_process_generator(context_id, generator)
        finally:
            # Unlike a WebSocket TTS, this HTTP generator is finished even if
            # it emitted no audio. Close its context normally at turn end.
            self._is_yielding_frames_synchronously = True

    async def _push_spoken_text(
        self, src_frame: AggregatedTextFrame, text: str, context_id: str, append_to_context: bool
    ):
        if self._synthesis_failed:
            # A broken stream has no reliable word alignment. Do not record
            # the entire sentence as spoken or replay audio already emitted.
            for frame in self._aggregated_frame_sequencer.complete_spoken_slot():
                await self.push_frame(frame)
            return
        await super()._push_spoken_text(src_frame, text, context_id, append_to_context)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, LLMFullResponseStartFrame):
            self.timing = frame.metadata.get("turn_timing")
        elif isinstance(frame, InterruptionFrame):
            if self.timing:
                await self.timing.finish("interrupted")
            self.timing = None
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseEndFrame) and self.timing:
            await self.timing.finish(
                "complete" if self.timing.first_audio_at is not None else "no-audio"
            )

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        self._synthesis_failed = False
        started = time.monotonic()
        first_audio_ms = None
        audio_bytes = 0
        status_code = None
        no_speakable_text = False
        outcome = "complete"
        timing = self.timing
        if timing:
            await timing.start_tts()
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
                status_code = response.status_code
                if status_code == 422:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 4096:
                            break
                    if len(body) <= 4096:
                        try:
                            detail = json.loads(body).get("detail")
                            no_speakable_text = (
                                isinstance(detail, dict)
                                and detail.get("code") == "no_speakable_text"
                            )
                        except (ValueError, AttributeError):
                            pass
                response.raise_for_status()
                async for audio in bounded_pcm(response):
                    if first_audio_ms is None:
                        first_audio_ms = round((time.monotonic() - started) * 1000)
                    audio_bytes += len(audio)
                    if timing:
                        await timing.first_audio()
                    yield TTSAudioRawFrame(audio, 24000, 1, context_id=context_id)
        except (httpx.HTTPError, ValueError) as error:
            self._synthesis_failed = True
            outcome = "failed"
            if timing:
                await timing.finish("failed")
            # Request-local content rejection must not disable future speech.
            # Credentials, endpoint and model configuration still require a fix.
            category = (
                ErrorCategory.APPLICATION
                if no_speakable_text
                else classify_http_status_code(status_code)
                if status_code is not None and status_code >= 400
                else ErrorCategory.CONNECTIVITY
                if isinstance(error, httpx.HTTPError)
                else ErrorCategory.SERVER
            )
            await self.push_error(
                "Speech synthesis failed",
                category=category,
            )
        except asyncio.CancelledError:
            outcome = "interrupted"
            if timing:
                await timing.finish("interrupted")
            raise
        finally:
            # No input text, response bodies, URLs, credentials or exceptions.
            logger.info(
                "TTS request context={} outcome={} status={} first_audio_ms={} "
                "elapsed_ms={} audio_bytes={}",
                context_id,
                outcome,
                status_code,
                first_audio_ms,
                round((time.monotonic() - started) * 1000),
                audio_bytes,
            )
