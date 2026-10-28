"""Constrain browser protobuf input to microphone audio and the ready handshake."""

import time

from pipecat.frames.frames import Frame, InputAudioRawFrame, InputTransportMessageFrame
from pipecat.serializers.protobuf import ProtobufFrameSerializer


class BrowserSerializer(ProtobufFrameSerializer):
    def __init__(self):
        super().__init__()
        self.ready = False
        self.budget = 64000.0
        self.updated = time.monotonic()

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
        # Never accept client-forged transcripts, assistant text or control frames.
        return None
