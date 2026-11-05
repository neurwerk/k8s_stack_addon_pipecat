"""Bounded, session-owned client for the inference manager's streaming protocol v1."""

import asyncio
import contextlib
import json
import re
import struct
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from simple_chatbot.activity import Activity as CallActivity
from simple_chatbot.config import Diarization
from simple_chatbot.speakers import SpeakerProfiles

RATE = 16000
MAX_PACKET_SAMPLES = 5120
MAX_QUEUED_BYTES = RATE * 2 * 30


class DiarizationError(ValueError):
    """Safe error category; never includes upstream payloads or credentials."""


def integer(message: dict, key: str) -> int:
    value = message.get(key)
    if type(value) is not int or not 0 <= value < 2**53:
        raise DiarizationError("Invalid diarization position")
    return value


@dataclass(frozen=True)
class Activity:
    start: int
    end: int
    speaker: str


@dataclass(frozen=True)
class SpeakerSpan:
    start: int
    end: int
    generation: int
    activities: tuple[Activity, ...]
    finalization_seconds: float | None = None

    def speaker(self, start: int, end: int) -> str | None:
        # Do not collapse overlap to a dominant speaker. Require enough attributed
        # audio to avoid naming a word based on a tiny sliver at its boundary.
        if not self.start <= start < end <= self.end:
            return None
        matched = [a for a in self.activities if a.start < end and a.end > start]
        ids = {a.speaker for a in matched}
        if len(ids) != 1:
            return None
        covered = 0
        cursor = start
        for item in sorted(matched, key=lambda a: a.start):
            left, right = max(start, item.start), min(end, item.end)
            covered += max(0, right - max(cursor, left))
            cursor = max(cursor, right)
        return next(iter(ids)) if covered >= (end - start) * 0.5 else None


@dataclass
class Operation:
    kind: str
    start: int = 0
    end: int = 0
    audio: bytes = b""
    future: asyncio.Future | None = None
    requested_at: float | None = None
    activity_token: int | None = None


class DiarizationSession:
    """One browser owner's model state, retained across voice connections.

    The actor serializes audio, flush and reset. No network wait occurs on the
    Pipecat audio path; the queue is bounded by both count and PCM bytes.
    """

    def __init__(self, settings: Diarization):
        self.settings = settings
        self.profiles = SpeakerProfiles(ttl=settings.profile_ttl_seconds)
        self.session_id: str | None = None
        self.position = 0  # Continuous application timeline, including across resets.
        self.base = 0  # Application position at the start of the remote session.
        self.accepted = 0
        self.sent = 0
        self.sequence = 0
        self.finalized = 0
        self.activities: deque[Activity] = deque()
        self.queue: asyncio.Queue[Operation] = asyncio.Queue(maxsize=2048)
        self.queued_bytes = 0
        self.socket: ClientConnection | None = None
        self.worker: asyncio.Task | None = None
        self.failed = False
        self.activity: CallActivity | None = None

    async def attach(self) -> None:
        if self.worker is not None:
            raise DiarizationError("Speaker session already attached")
        if self.profiles.expired or self.failed:
            await self.delete()
        try:
            await self._connect()
        except DiarizationError as error:
            # Expired model state must never inherit old names or numbering.
            if str(error) != "session_expired":
                raise
            self._clear()
            await self._connect()
        self.profiles.touch()
        self.profiles.current_speaker = None
        self.profiles.pending_speakers.clear()
        self.worker = asyncio.create_task(self._run())

    def feed(self, audio: bytes) -> None:
        if not audio:
            return
        if self.activity and not self.activity.operations["diarization"]:
            self.activity.set("diarization", "active", "Streaming microphone audio")
        if len(audio) % 2 or self.failed or self.worker is None or self.worker.done():
            raise DiarizationError("Speaker audio unavailable")
        if self.queued_bytes + len(audio) > MAX_QUEUED_BYTES:
            raise DiarizationError("Speaker audio queue full")
        for offset in range(0, len(audio), MAX_PACKET_SAMPLES * 2):
            chunk = audio[offset : offset + MAX_PACKET_SAMPLES * 2]
            end = self.position + len(chunk) // 2
            self._enqueue(Operation("audio", self.position, end, chunk))
            self.position = end
            self.queued_bytes += len(chunk)
        self.profiles.touch()

    def flush(self, start: int) -> asyncio.Future[SpeakerSpan]:
        future: asyncio.Future[SpeakerSpan] = asyncio.get_running_loop().create_future()
        # A canceled STT request may no longer await this result. Consume exceptions
        # while preserving them for callers that do await it.
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        token = (
            self.activity.begin("diarization", "Finalizing speaker results")
            if self.activity
            else None
        )
        try:
            self._enqueue(
                Operation(
                    "flush",
                    start,
                    self.position,
                    future=future,
                    requested_at=time.monotonic(),
                    activity_token=token,
                )
            )
        except DiarizationError:
            if self.activity and token is not None:
                self.activity.end("diarization", token, "failed", "Speaker queue unavailable")
            raise
        return future

    def _enqueue(self, operation: Operation) -> None:
        if self.failed:
            raise DiarizationError("Speaker session failed")
        try:
            self.queue.put_nowait(operation)
        except asyncio.QueueFull:
            raise DiarizationError("Speaker audio queue full") from None

    async def reset(self) -> None:
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        self._enqueue(Operation("reset", future=future))
        await asyncio.wait_for(asyncio.shield(future), self.settings.timeout_seconds * 2)

    async def detach(self) -> None:
        try:
            if self.worker and not self.worker.done() and not self.failed:
                await asyncio.wait_for(
                    asyncio.shield(self.flush(self.position)), self.settings.timeout_seconds * 2
                )
        except (DiarizationError, TimeoutError):
            self.failed = True
        except asyncio.CancelledError:
            # The actor may still own unacknowledged audio. Draining it creates a
            # gap in our local timeline; never resume that state as if complete.
            self.failed = True
            self.profiles.clear()
            raise
        finally:
            if self.worker:
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker
                self.worker = None
            self._drain()
            await self._disconnect()

    async def delete(self) -> None:
        await self.detach()
        try:
            if self.session_id:
                await self._connect()
                await self._command({"type": "delete"}, "deleted")
        except (OSError, WebSocketException, TimeoutError, DiarizationError):
            # Server TTL is the final cleanup boundary when the service is down.
            pass
        finally:
            await self._disconnect()
            self._clear()

    def _clear(self) -> None:
        self.base = self.position
        self.accepted = self.sent = self.sequence = self.finalized = 0
        self.session_id = None
        self.activities.clear()
        self.profiles.clear()
        self.failed = False

    async def _disconnect(self) -> None:
        if self.socket:
            await self.socket.close()
            self.socket = None

    async def _connect(self) -> None:
        await self._disconnect()
        async with asyncio.timeout(self.settings.timeout_seconds):
            self.socket = await connect(
                self.settings.url,
                additional_headers={"Authorization": "Bearer " + self.settings.key.read()},
                open_timeout=self.settings.timeout_seconds,
                close_timeout=2,
                max_size=65536,
                max_queue=16,
                compression=None,
                proxy=None,
            )
            resume = self.session_id is not None
            await self.socket.send(
                json.dumps(
                    {
                        "type": "resume",
                        "version": 1,
                        "session_id": self.session_id,
                        "after_result_seq": self.sequence,
                    }
                    if resume
                    else {
                        "type": "start",
                        "version": 1,
                        "encoding": "pcm_s16le",
                        "sample_rate_hz": RATE,
                        "channels": 1,
                        "buffer_profile": "ultra_low_latency",
                    }
                )
            )
            message = await self._receive()
            if message.get("type") != ("resumed" if resume else "started"):
                raise DiarizationError("Invalid speaker session handshake")
            session_id = message.get("session_id")
            if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
                raise DiarizationError("Invalid speaker session identity")
            if resume and session_id != self.session_id:
                raise DiarizationError("Speaker identity changed")
            self.session_id = session_id
            accepted = integer(message, "next_sample")
            sequence = integer(message, "last_result_seq")
            finalized = integer(message, "finalized_through_sample")
            if not self.accepted <= accepted <= self.sent or sequence < self.sequence:
                raise DiarizationError("Speaker resume position mismatch")
            if not self.finalized <= finalized <= accepted:
                raise DiarizationError("Invalid speaker finalization")
            self.accepted = accepted
            while self.sequence < sequence:
                await self._event(await self._receive())
            self.finalized = finalized

    async def _receive(self) -> dict:
        assert self.socket
        data = await self.socket.recv()
        if not isinstance(data, str):
            raise DiarizationError("Invalid diarization message")
        try:
            message = json.loads(data)
        except (ValueError, RecursionError):
            raise DiarizationError("Invalid diarization JSON") from None
        if not isinstance(message, dict):
            raise DiarizationError("Invalid diarization message")
        if message.get("type") == "error":
            code = message.get("code")
            raise DiarizationError(
                code
                if code in {"session_expired", "replay_gap", "session_busy"}
                else "Diarization service rejected request"
            )
        return message

    async def _event(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "activity":
            seq = integer(message, "seq")
            if seq != self.sequence + 1 or message.get("session_id") != self.session_id:
                raise DiarizationError("Speaker result sequence mismatch")
            start, end = integer(message, "start_sample"), integer(message, "end_sample")
            speaker = message.get("speaker_id")
            if (
                not self.finalized <= start < end <= self.sent
                or not isinstance(speaker, str)
                or not re.fullmatch(r"speaker_[0-7]", speaker)
                or message.get("state") != "final"
            ):
                raise DiarizationError("Invalid speaker activity")
            if len(self.activities) >= 24000:
                raise DiarizationError("Speaker result buffer full")
            self.activities.append(Activity(self.base + start, self.base + end, speaker))
            self.sequence = seq
            # Activity does NOT advance finalization: overlapping entries can follow.
        elif kind in {"progress", "ack"}:
            accepted_key = "next_sample" if kind == "ack" else "accepted_through_sample"
            accepted = integer(message, accepted_key)
            if not self.accepted <= accepted <= self.sent:
                raise DiarizationError("Invalid speaker acknowledgement")
            self.accepted = accepted
            if kind == "progress":
                finalized = integer(message, "finalized_through_sample")
                if not self.finalized <= finalized <= accepted:
                    raise DiarizationError("Invalid speaker finalization")
                self.finalized = finalized
        else:
            raise DiarizationError("Unexpected diarization event")

    async def _command(self, command: dict, expected: str) -> dict:
        assert self.socket
        async with asyncio.timeout(self.settings.timeout_seconds):
            await self.socket.send(json.dumps(command))
            while True:
                message = await self._receive()
                if message.get("type") == expected:
                    return message
                await self._event(message)

    async def _perform(self, operation: Operation) -> Any:
        assert self.socket
        async with asyncio.timeout(self.settings.timeout_seconds):
            if operation.kind == "audio":
                start, end = operation.start - self.base, operation.end - self.base
                if not start <= self.accepted <= end:
                    raise DiarizationError("Speaker audio position mismatch")
                if self.accepted == end:
                    return None  # Server accepted the packet before a lost acknowledgement.
                audio = operation.audio[(self.accepted - start) * 2 :]
                self.sent = max(self.sent, end)
                await self.socket.send(struct.pack("<Q", self.accepted) + audio)
                while self.accepted < end:
                    await self._event(await self._receive())
            elif operation.kind == "flush":
                end = operation.end - self.base
                request_id = str(operation.end)
                message = await self._command(
                    {"type": "flush", "through_sample": end, "request_id": request_id},
                    "flushed",
                )
                if (
                    message.get("request_id") != request_id
                    or integer(message, "next_sample") != end
                    or integer(message, "finalized_through_sample") != end
                    or end < self.finalized
                ):
                    raise DiarizationError("Incomplete speaker flush")
                self.finalized = end
                span = SpeakerSpan(
                    operation.start,
                    operation.end,
                    self.profiles.generation,
                    tuple(a for a in self.activities if a.end > operation.start),
                    # Include queued audio and reconnects; stop before the STT consumer resumes.
                    time.monotonic() - operation.requested_at
                    if operation.requested_at is not None
                    else None,
                )
                # Keep at most one minute of results for subsequent STT pre-roll.
                cutoff = operation.end - RATE * 60
                self.activities = deque(a for a in self.activities if a.end > cutoff)
                return span
            elif operation.kind == "reset":
                await self._command({"type": "delete"}, "deleted")
                accepted_position = self.base + self.accepted
                self._clear()
                self.base = accepted_position
                await self._connect()
            else:
                raise DiarizationError("Invalid speaker operation")
        return None

    async def _run(self) -> None:
        operation: Operation | None = None
        try:
            while True:
                operation = await self.queue.get()
                try:
                    result = await self._perform(operation)
                except (OSError, ConnectionClosed, TimeoutError):
                    # Reset is a mutation. A lost reply cannot safely be retried.
                    if operation.kind == "reset":
                        raise DiarizationError("Speaker reset interrupted") from None
                    await self._connect()
                    result = await self._perform(operation)
                self.queued_bytes -= len(operation.audio)
                if self.activity and operation.activity_token is not None:
                    self.activity.end(
                        "diarization",
                        operation.activity_token,
                        "active",
                        "Streaming microphone audio",
                    )
                if operation.future and not operation.future.done():
                    operation.future.set_result(result)
                operation = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            self.profiles.clear()
        finally:
            if self.activity and operation and operation.activity_token is not None:
                self.activity.end(
                    "diarization",
                    operation.activity_token,
                    "failed" if self.failed else "interrupted",
                    "Failed" if self.failed else "Interrupted",
                )
            if operation and operation.future and not operation.future.done():
                operation.future.set_exception(DiarizationError("Speaker operation interrupted"))
            self._drain()

    def _drain(self) -> None:
        while not self.queue.empty():
            operation = self.queue.get_nowait()
            if operation.future and not operation.future.done():
                operation.future.set_exception(DiarizationError("Speaker session closed"))
            if self.activity and operation.activity_token is not None:
                self.activity.end(
                    "diarization", operation.activity_token, "interrupted", "Interrupted"
                )
        self.queued_bytes = 0
