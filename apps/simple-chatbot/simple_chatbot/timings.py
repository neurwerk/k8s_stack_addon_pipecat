"""Per-call numeric timing snapshots; never retain transcript or audio content."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

TimingSink = Callable[[dict], Awaitable[None]]


@dataclass
class TurnTiming:
    turn: int
    kind: str
    send: TimingSink
    status: str = "transcribing"
    values: dict[str, float] = field(default_factory=dict)
    llm_started: float | None = None
    first_text_at: float | None = None
    tts_started: float | None = None
    first_audio_at: float | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("failed", "interrupted", "complete", "no-audio")

    async def publish(self) -> None:
        await self.send(
            {
                "type": "turn-timing",
                "turn": self.turn,
                "kind": self.kind,
                "status": self.status,
                **{key: round(value, 3) for key, value in self.values.items()},
            }
        )

    async def transcribed(self, seconds: float, words: int, audio_seconds: float | None) -> None:
        self.status = "transcribed"
        self.values["stt_ms"] = self.values.get("stt_ms", 0) + seconds * 1000
        self.values["words"] = self.values.get("words", 0) + words
        if audio_seconds is not None:
            self.values["audio_seconds"] = self.values.get("audio_seconds", 0) + audio_seconds
        await self.publish()

    async def first_text(self) -> None:
        if self.finished or self.first_text_at is not None or self.llm_started is None:
            return
        self.first_text_at = time.monotonic()
        self.values["llm_first_text_ms"] = (self.first_text_at - self.llm_started) * 1000
        await self.publish()

    async def start_tts(self) -> None:
        if self.finished or self.tts_started is not None:
            return
        self.tts_started = time.monotonic()
        if self.first_text_at is not None:
            self.values["text_buffer_ms"] = (self.tts_started - self.first_text_at) * 1000
        self.status = "synthesizing"
        await self.publish()

    async def first_audio(self) -> None:
        if self.finished or self.first_audio_at is not None or self.tts_started is None:
            return
        self.first_audio_at = time.monotonic()
        self.values["tts_first_audio_ms"] = (self.first_audio_at - self.tts_started) * 1000
        if self.first_text_at is not None:
            self.values["text_to_audio_ms"] = (self.first_audio_at - self.first_text_at) * 1000
        self.status = "audio-ready"
        await self.publish()

    async def finish(self, status: str) -> None:
        if self.finished:
            return
        self.status = status
        await self.publish()


class TurnTimings:
    def __init__(self, send: TimingSink):
        self.send = send
        self.sequence = 0
        self.pending: TurnTiming | None = None

    def _new(self, kind: str) -> TurnTiming:
        self.sequence += 1
        return TurnTiming(self.sequence, kind, self.send)

    async def begin_stt(self) -> TurnTiming:
        if self.pending is None:
            self.pending = self._new("reply")
        self.pending.status = "transcribing"
        await self.pending.publish()
        return self.pending

    async def begin_llm(self) -> TurnTiming:
        turn = self.pending or self._new("greeting" if self.sequence == 0 else "reply")
        self.pending = None
        turn.status = "generating"
        await turn.publish()
        return turn
