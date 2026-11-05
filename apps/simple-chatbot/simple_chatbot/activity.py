"""Per-call, content-free activity snapshots, independent of the media path."""

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

STAGES = (
    "input",
    "vad",
    "turn",
    "stt",
    "diarization",
    "attribution",
    "ready",
    "llm",
    "tool",
    "buffer",
    "tts",
    "output",
)
RESPONSE_STAGES = ("llm", "tool", "buffer", "tts", "output")


class Activity:
    def __init__(self):
        self.started = time.monotonic()
        self.sequence = 0
        self.states: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=160)
        self.operations: dict[str, dict[int, str]] = {stage: {} for stage in STAGES}
        self.next_operation = 0
        self.models: dict[str, dict] = {}
        self.interruption: dict | None = None
        self.last_audio: float | None = None
        self.speaking = False
        self.turn_complete = False
        self.transcript_ready = False
        self.closed = False
        self.paused = False
        self.generation = 0
        self._disabled: set[str] = set()
        for stage in STAGES:
            self.set(stage, "waiting", "Awaiting input")

    def now(self) -> float:
        return round((time.monotonic() - self.started) * 1000, 3)

    def set(self, stage: str, status: str, label: str) -> None:
        if self.closed or self.paused:
            return
        previous = self.states.get(stage, {})
        if (previous.get("status"), previous.get("label")) == (status, label):
            return
        self.sequence += 1
        event = dict(stage=stage, status=status, label=label, at_ms=self.now(), seq=self.sequence)
        self.states[stage] = event
        self.events.append(event)

    def begin(self, stage: str, label: str) -> int:
        self.next_operation += 1
        token = self.next_operation
        if self.paused or self.closed:
            return token
        self.operations[stage][token] = label
        self.set(stage, "active", label)
        return token

    def end(
        self, stage: str, token: int, status: str = "complete", label: str = "Complete"
    ) -> None:
        pending = self.operations[stage]
        if token not in pending:
            return  # A canceled operation cannot overwrite the next response.
        del pending[token]
        if pending:
            self.set(stage, "active", next(reversed(pending.values())))
        elif self.states[stage]["status"] not in ("failed", "interrupted"):
            self.set(stage, status, label)

    @contextlib.contextmanager
    def work(self, stage: str, label: str):
        token = self.begin(stage, label)
        try:
            yield token
        except asyncio.CancelledError:
            self.end(stage, token, "interrupted", "Interrupted")
            raise
        except Exception:
            self.end(stage, token, "failed", "Failed")
            raise
        else:
            self.end(stage, token)

    def gate(self) -> None:
        if self.speaking:
            label = "User is speaking"
        elif not self.turn_complete:
            label = "Waiting for turn completion"
        elif not self.transcript_ready:
            label = "Waiting for attributed transcript"
        else:
            self.set("ready", "complete", "Ready to answer")
            return
        self.set("ready", "waiting", label)

    def audio(self) -> None:
        if self.paused or self.closed:
            return
        self.last_audio = time.monotonic()
        self.set("input", "active", "Receiving microphone audio")
        if not self.speaking:
            self.set("vad", "active", "Monitoring silence")

    def interrupt(self) -> None:
        if self.paused or self.closed:
            return
        busy = any(self.states[s]["status"] == "active" for s in RESPONSE_STAGES)
        if not busy:
            return
        output_active = self.states["output"]["status"] == "active"
        self.interruption = {"at_ms": self.now(), "output_was_active": output_active}
        for stage in RESPONSE_STAGES:
            self.operations[stage].clear()
            if stage != "output" and self.states[stage]["status"] in ("active", "waiting"):
                self.set(stage, "interrupted", "Interruption requested")
        if output_active:
            self.set("output", "active", "Stopping output")

    def pause(self) -> None:
        if self.paused or self.closed:
            return
        self.generation += 1
        self._disabled = {s for s in STAGES if self.states[s]["status"] == "off"}
        self.interruption = None
        self.last_audio = None
        self.speaking = self.turn_complete = self.transcript_ready = False
        for stage in STAGES:
            self.operations[stage].clear()
            if stage not in self._disabled:
                self.set(stage, "off", "Conversation paused")
        self.paused = True

    def resume(self) -> None:
        if not self.paused or self.closed:
            return
        self.paused = False
        self.speaking = self.turn_complete = self.transcript_ready = False
        for stage in STAGES:
            if stage not in self._disabled:
                self.set(stage, "waiting", "Awaiting fresh input")

    def snapshot(self) -> dict:
        return {
            "type": "pipeline-activity",
            "at_ms": self.now(),
            "seq": self.sequence,
            "paused": self.paused,
            "states": {key: dict(value) for key, value in self.states.items()},
            "events": [dict(event) for event in self.events],
            "models": {key: dict(value) for key, value in self.models.items()},
            "interruption": dict(self.interruption) if self.interruption else None,
        }

    async def publish(self, send: Callable[[dict], Awaitable[None]]) -> None:
        while True:
            if self.last_audio is not None and time.monotonic() - self.last_audio > 1:
                self.set("input", "waiting", "No recent microphone audio")
                self.set("vad", "waiting", "Waiting for audio")
            # Sending can never back-pressure microphone or model processing.
            # Periodic full snapshots also recover from a lost UI update.
            try:
                async with asyncio.timeout(1):
                    await send(self.snapshot())
            except (TimeoutError, OSError, RuntimeError):
                pass
            await asyncio.sleep(0.2)

    def close(self) -> None:
        self.paused = False
        for stage in STAGES:
            self.operations[stage].clear()
            if stage not in self._disabled and self.states[stage]["status"] in (
                "active",
                "waiting",
                "off",
            ):
                self.set(stage, "off", "Conversation ended")
        self.closed = True


class ActivityObserver(BaseObserver):
    def __init__(self, activity: Activity):
        super().__init__(observe_every_push=False)
        self.activity = activity

    async def on_push_frame(self, data: FramePushed):
        a, frame = self.activity, data.frame
        if a.paused or a.closed:
            return
        if isinstance(frame, InputAudioRawFrame):
            a.audio()
        elif isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            # The aggregator emits an upstream copy and queues a downstream copy.
            if data.direction != FrameDirection.UPSTREAM:
                return
            a.speaking = isinstance(frame, VADUserStartedSpeakingFrame)
            if a.speaking:
                a.turn_complete = a.transcript_ready = False
                a.set("vad", "active", "Speech detected")
                a.set("turn", "active", "Collecting speech")
            else:
                a.set("vad", "active", "Pause detected; monitoring")
            a.gate()
        elif data.direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, UserStartedSpeakingFrame):
                a.turn_complete = a.transcript_ready = False
                a.gate()
            elif isinstance(frame, InterruptionFrame):
                a.interrupt()
            elif isinstance(frame, BotStartedSpeakingFrame):
                a.set("output", "active", "Sending speech audio")
            elif isinstance(frame, BotStoppedSpeakingFrame):
                if (
                    a.interruption
                    and a.interruption.get("output_was_active")
                    and ("output_stopped_ms" not in a.interruption)
                ):
                    a.interruption["output_stopped_ms"] = a.now()
                    a.set("output", "interrupted", "Server output stopped")
                else:
                    a.set("output", "complete", "Server output finished")


class ObservedSmartTurn(LocalSmartTurnAnalyzerV3):
    def __init__(self, activity: Activity):
        super().__init__()
        self.activity = activity

    async def analyze_end_of_turn(self):
        a = self.activity
        generation = a.generation
        with a.work("turn", "Checking end of turn"):
            state, prediction = await super().analyze_end_of_turn()
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

        if a.paused or generation != a.generation:
            return state, prediction
        a.turn_complete = state == EndOfTurnState.COMPLETE
        a.set(
            "turn",
            "complete" if a.turn_complete else "waiting",
            "Turn complete" if a.turn_complete else "Waiting for continuation",
        )
        a.gate()
        return state, prediction
