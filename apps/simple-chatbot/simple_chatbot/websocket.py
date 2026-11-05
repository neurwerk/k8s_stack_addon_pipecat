"""Constrain browser protobuf input to microphone audio and bounded control."""

import time

from pipecat.frames.frames import Frame, InputAudioRawFrame, InputTransportMessageFrame
from pipecat.serializers.protobuf import ProtobufFrameSerializer


class BrowserSerializer(ProtobufFrameSerializer):
    def __init__(self):
        super().__init__()
        self.ready = False
        self.budget = 64000.0
        self.updated = time.monotonic()
        self.control_tokens = 16.0
        self.control_updated = self.updated

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if not isinstance(data, bytes) or len(data) > 65536:
            raise ValueError("Invalid browser media message")
        frame = await super().deserialize(data)
        if isinstance(frame, InputAudioRawFrame):
            now = time.monotonic()
            self.budget = min(64000, self.budget + (now - self.updated) * 32000)
            self.updated = now
            self.budget -= len(frame.audio)
            if (
                frame.sample_rate != 16000
                or frame.num_channels != 1
                or len(frame.audio) > 32000
                or len(frame.audio) % 2
                or self.budget < 0
            ):
                raise ValueError("Invalid browser audio format")
            return frame
        if isinstance(frame, InputTransportMessageFrame):
            message = frame.message
            if (
                not self.ready
                and isinstance(message, dict)
                and message.get("type") == "client-ready"
            ):
                self.ready = True
                return frame
            if (
                self.ready
                and isinstance(message, dict)
                and set(message) == {"id", "label", "type", "data"}
                and message.get("label") == "rtvi-ai"
                and message.get("type") == "client-message"
                and isinstance(message.get("id"), str)
                and len(message["id"]) <= 128
                and isinstance(message.get("data"), dict)
                and set(message["data"]) == {"t", "d"}
                and message["data"]["t"] == "conversation-control"
                and isinstance(message["data"]["d"], dict)
                and set(message["data"]["d"]) == {"action", "sequence"}
                and message["data"]["d"]["action"] in ("pause", "resume")
                and type(message["data"]["d"]["sequence"]) is int
                and 0 <= message["data"]["d"]["sequence"] <= 1000000
            ):
                now = time.monotonic()
                self.control_tokens = min(
                    16, self.control_tokens + (now - self.control_updated) * 8
                )
                self.control_updated = now
                if self.control_tokens < 1:
                    return None
                self.control_tokens -= 1
                return frame
        # Never accept client-forged transcripts, assistant text or control frames.
        return None
