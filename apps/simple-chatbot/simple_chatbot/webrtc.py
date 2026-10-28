"""Bound the pinned SmallWebRTC data-channel path before its pending queue and RTVI."""

import asyncio
import inspect
import json
import re
import time

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection


class BrowserMessages:
    def __init__(self) -> None:
        self.ready = False
        self.tokens = 16.0
        self.updated = time.monotonic()

    def accept(self, raw: object) -> bool:
        now = time.monotonic()
        self.tokens = min(16, self.tokens + (now - self.updated) * 8)
        self.updated = now
        self.tokens -= 1
        if self.tokens < 0 or not isinstance(raw, str) or len(raw.encode()) > 4096:
            return False
        if re.fullmatch(r"ping: [0-9]{10,16}", raw):
            return True
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            return False
        if not isinstance(value, dict):
            return False
        if value.get("type") == "signalling":
            message = value.get("message")
            return (
                set(value) == {"type", "message"}
                and isinstance(message, dict)
                and set(message) == {"type", "receiver_index", "enabled"}
                and message["type"] == "trackStatus"
                and type(message["receiver_index"]) is int
                and 0 <= message["receiver_index"] <= 2
                and type(message["enabled"]) is bool
            )
        data = value.get("data")
        if (
            self.ready
            or set(value) - {"id", "label", "type", "data"}
            or value.get("label") != "rtvi-ai"
            or value.get("type") != "client-ready"
            or not isinstance(value.get("id"), str)
            or len(value["id"]) > 128
            or not isinstance(data, dict)
            or set(data) - {"version", "about"}
            or not isinstance(data.get("version"), str)
            or len(data["version"]) > 32
            or ("about" in data and not isinstance(data["about"], dict))
        ):
            return False
        self.ready = True
        return True


class BrowserWebRTCConnection(SmallWebRTCConnection):
    def __init__(self, **kwargs):
        self.browser_messages = BrowserMessages()
        self.inbound_tasks: set[asyncio.Future] = set()
        super().__init__(**kwargs)

    def _setup_listeners(self):
        super()._setup_listeners()
        upstream_channels = tuple(self._pc.listeners("datachannel"))
        self._pc.remove_all_listeners("datachannel")
        channel_seen = False

        @self._pc.on("datachannel")
        def guard_channel(channel):
            nonlocal channel_seen
            if channel_seen:
                channel.close()
                return
            channel_seen = True
            for handler in upstream_channels:
                handler(channel)
            # Replace the pinned upstream callback before any message can enter its queue.
            handlers = tuple(channel.listeners("message"))
            channel.remove_all_listeners("message")

            @channel.on("message")
            def bounded_message(raw):
                if len(self.inbound_tasks) >= 16 or not self.browser_messages.accept(raw):
                    return
                for handler in handlers:
                    result = handler(raw)
                    if inspect.isawaitable(result):
                        task = asyncio.ensure_future(result)
                        self.inbound_tasks.add(task)
                        task.add_done_callback(self.inbound_tasks.discard)

    async def disconnect(self):
        tasks = list(self.inbound_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().disconnect()
