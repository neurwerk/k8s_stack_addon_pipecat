"""Reviewed speaker mutations and streamed speech boundaries, without service calls."""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from pipecat.frames.frames import LLMContextFrame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from test_security import settings
from test_streams import Chunks, event, sse

from simple_chatbot.notices import MARKER
from simple_chatbot.services import GatewayLLM, bounded_sse
from simple_chatbot.speakers import SpeakerProfiles


def tool_event(
    name="remember_speaker", arguments='{"name":"Alice","quote":"My name is Alice"}', *, index=0
):
    return {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": "call_synthetic",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }


def tool_response(name="remember_speaker", arguments='{"name":"Alice","quote":"My name is Alice"}'):
    return sse(tool_event(name, arguments), event(None, finish="tool_calls"))


def response(payload):
    return httpx.Response(
        200, stream=Chunks([payload]), headers={"content-type": "text/event-stream"}
    )


class SpeakerToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profiles = SpeakerProfiles()
        self.profiles.observe(
            [("speaker_2", word) for word in ("My", "name", "is", "Alice")],
            ["speaker_2"] * 4,
        )
        self.requests = []
        self.notices = []
        self.context = LLMContext(
            [
                {"role": "system", "content": "Speak briefly."},
                {"role": "user", "content": "My name is Alice."},
            ]
        )

    async def run_response(self, replies, *, profiles=True, forget=None, on_request=None):
        replies = iter(replies)

        def respond(request):
            self.requests.append(json.loads(request.content))
            if on_request:
                on_request(len(self.requests))
            reply = next(replies)
            return response(reply) if isinstance(reply, bytes) else reply

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            llm = GatewayLLM(
                settings(Path("unused")),
                http,
                "synthetic-token",
                self.notices.append,
                profiles=self.profiles if profiles else None,
                forget_speakers=forget,
            )
            llm.push_frame = AsyncMock()
            llm.push_error = AsyncMock()
            await llm.process_frame(LLMContextFrame(self.context), FrameDirection.DOWNSTREAM)
        self.llm = llm
        return "".join(
            call.args[0].text
            for call in llm.push_frame.await_args_list
            if isinstance(call.args[0], LLMTextFrame)
        )

    async def test_fragmented_tool_binds_current_speaker_and_only_followup_is_spoken(self):
        original = json.dumps(self.context.get_messages())
        first = tool_event(arguments='{"na')
        tail = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": 'me":"Alice","quote":"My name is Alice"}'
                                },
                            }
                        ]
                    },
                }
            ]
        }
        spoken = await self.run_response(
            [
                sse(first, tail, event(None, finish="tool_calls")),
                sse(
                    event("<think>private</think>Hello, Alice." + MARKER + "notice"),
                    event(None, finish="stop"),
                ),
            ]
        )
        self.assertEqual(self.profiles.names, {"speaker_2": "Alice"})
        self.assertEqual(spoken, "Hello, Alice.")
        self.assertEqual(self.notices, ["notice"])
        self.llm.push_error.assert_not_awaited()
        self.assertEqual(len(self.requests), 2)
        first_request, followup = self.requests
        self.assertEqual(len(first_request["tools"]), 2)
        self.assertFalse(first_request["parallel_tool_calls"])
        self.assertNotIn("tools", followup)
        self.assertTrue(all(item["stream"] for item in self.requests))
        self.assertNotIn('"Alice"', first_request["messages"][0]["content"])
        self.assertEqual(followup["messages"][0]["content"], self.profiles.instructions())
        assistant, tool = followup["messages"][-2:]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "remember_speaker")
        self.assertEqual(tool["tool_call_id"], assistant["tool_calls"][0]["id"])
        self.assertEqual(json.loads(tool["content"])["speaker_id"], "speaker_2")
        self.assertEqual(json.dumps(self.context.get_messages()), original)

    async def test_unknown_extra_arbitrary_ids_and_malformed_arguments_cannot_mutate(self):
        for name, arguments in (
            (
                "remember_speaker",
                '{"name":"Alice","quote":"My name is Alice","speaker_id":"speaker_7"}',
            ),
            ("remember_speaker", '{"name":"Alice","quote":"My name is Alice","turn":1}'),
            ("remember_speaker", '{"speaker_id":"speaker_7"}'),
            ("remember_speaker", '{"name":123}'),
            ("remember_speaker", "[]"),
            ("remember_speaker", "{"),
            ("forget_speakers", '{"speaker_id":"speaker_2"}'),
            ("delete_everything", "{}"),
        ):
            with self.subTest(name=name, arguments=arguments):
                self.requests.clear()
                forget = AsyncMock()
                spoken = await self.run_response(
                    [
                        tool_response(name, arguments),
                        sse(event("Please try again.", finish="stop")),
                    ],
                    forget=forget,
                )
                self.assertEqual(spoken, "Please try again.")
                self.assertEqual(self.profiles.names, {})
                forget.assert_not_awaited()
                result = json.loads(self.requests[1]["messages"][-1]["content"])
                self.assertFalse(result["ok"])

    async def test_stale_turn_or_generation_rejects_register_and_forget(self):
        for name, arguments in (
            ("remember_speaker", '{"name":"Alice","quote":"My name is Alice"}'),
            ("forget_speakers", "{}"),
        ):
            for change in (
                lambda: self.profiles.observe([("speaker_7", "Hi")]),
                self.profiles.clear,
            ):
                with self.subTest(name=name, change=change):
                    self.requests.clear()
                    self.profiles.observe([("speaker_2", "Hi")])
                    forget = AsyncMock()
                    await self.run_response(
                        [
                            tool_response(name, arguments),
                            sse(event("Please ask again.", finish="stop")),
                        ],
                        forget=forget,
                        on_request=lambda count, change=change: change() if count == 1 else None,
                    )
                    self.assertEqual(self.profiles.names, {})
                    forget.assert_not_awaited()
                    self.assertFalse(json.loads(self.requests[1]["messages"][-1]["content"])["ok"])

    async def test_ambiguous_or_unknown_current_speaker_is_not_registered(self):
        for speakers in (["speaker_1", "speaker_2"], [None], ["arbitrary-id"]):
            with self.subTest(speakers=speakers):
                self.requests.clear()
                self.profiles.begin_response()
                self.profiles.observe(
                    [
                        (speaker, word)
                        for speaker, word in zip(speakers, ("My", "name"), strict=False)
                    ]
                )
                await self.run_response(
                    [
                        tool_response(),
                        sse(event("Please speak separately.", finish="stop")),
                    ]
                )
                self.assertEqual(self.profiles.names, {})
                self.assertFalse(json.loads(self.requests[1]["messages"][-1]["content"])["ok"])

    async def test_forget_callback_success_and_failure_are_reported_truthfully(self):
        self.profiles.names["speaker_2"] = "Alice"
        forget = AsyncMock(side_effect=self.profiles.clear)
        spoken = await self.run_response(
            [
                tool_response("forget_speakers", "{}"),
                sse(event("Profiles forgotten.", finish="stop")),
            ],
            forget=forget,
        )
        forget.assert_awaited_once()
        self.assertEqual(spoken, "Profiles forgotten.")
        self.assertEqual(self.profiles.names, {})
        self.assertEqual(json.loads(self.requests[1]["messages"][-1]["content"]), {"ok": True})
        self.requests.clear()
        forget = AsyncMock(side_effect=RuntimeError("private backend failure"))
        spoken = await self.run_response(
            [
                tool_response("forget_speakers", "{}"),
                sse(event("I could not forget them.", finish="stop")),
            ],
            forget=forget,
        )
        forget.assert_awaited_once()
        result = self.requests[1]["messages"][-1]["content"]
        self.assertFalse(json.loads(result)["ok"])
        self.assertNotIn("private", result + spoken)

    async def test_incomplete_completion_never_executes_mutation(self):
        forget = AsyncMock()
        incomplete = sse(
            tool_event("forget_speakers", "{}"), event(None, finish="tool_calls"), done=False
        )
        spoken = await self.run_response([incomplete, incomplete], forget=forget)
        self.assertEqual(spoken, "")
        forget.assert_not_awaited()
        self.llm.push_error.assert_awaited_once()

    async def test_followup_failure_never_replays_completed_mutation(self):
        forget = AsyncMock(side_effect=self.profiles.clear)
        await self.run_response(
            [
                tool_response("forget_speakers", "{}"),
                sse(event(""), done=False),
            ],
            forget=forget,
        )
        forget.assert_awaited_once()
        self.assertEqual(len(self.requests), 2)
        self.llm.push_error.assert_awaited_once()

    async def test_cancellation_during_forget_never_retries_or_speaks_success(self):
        started = asyncio.Event()

        async def forget():
            self.profiles.clear()
            started.set()
            await asyncio.Event().wait()

        callback = AsyncMock(side_effect=forget)
        task = asyncio.create_task(
            self.run_response(
                [
                    tool_response("forget_speakers", "{}"),
                ],
                forget=callback,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        callback.assert_awaited_once()
        self.assertEqual(len(self.requests), 1)

    async def test_second_tool_round_is_rejected_without_another_mutation(self):
        forget = AsyncMock()
        await self.run_response(
            [tool_response(), tool_response("forget_speakers", "{}")], forget=forget
        )
        self.assertEqual(self.profiles.names, {"speaker_2": "Alice"})
        forget.assert_not_awaited()
        self.assertEqual(len(self.requests), 2)
        self.llm.push_error.assert_awaited_once()

    async def test_enabled_normal_speech_is_streamed_without_an_extra_request(self):
        spoken = await self.run_response([sse(event("Hello.", finish="stop"))])
        self.assertEqual(spoken, "Hello.")
        self.assertEqual(len(self.requests), 1)
        self.llm.push_error.assert_not_awaited()

    async def test_disabled_path_preserves_request_and_speech_filters(self):
        spoken = await self.run_response(
            [
                sse(event("<think>hidden</think>Hello." + MARKER + "notice", finish="stop")),
            ],
            profiles=False,
        )
        self.assertEqual(spoken, "Hello.")
        self.assertEqual(self.notices, ["notice"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["messages"], self.context.get_messages())
        self.assertNotIn("tools", self.requests[0])
        self.assertNotIn("parallel_tool_calls", self.requests[0])
        self.llm.push_error.assert_not_awaited()


class SpeakerSSETests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_completion_is_explicitly_opt_in(self):
        with self.assertRaises(ValueError):
            _ = [item async for item in bounded_sse(response(tool_response()))]
        parsed = [item async for item in bounded_sse(response(tool_response()), allow_tools=True)]
        self.assertEqual(len(parsed), 2)

    async def test_opt_in_still_requires_complete_bounded_single_call(self):
        multiple = tool_event()
        multiple["choices"][0]["delta"]["tool_calls"].append(
            tool_event(index=1)["choices"][0]["delta"]["tool_calls"][0]
        )
        for payload in (
            sse(tool_event(), event(None, finish="stop")),
            sse(tool_event(), event(None, finish="length")),
            sse(tool_event(), event(None, finish="tool_calls"), done=False),
            sse(event(None, finish="tool_calls")),
            sse(tool_event(index=1), event(None, finish="tool_calls")),
            sse(multiple, event(None, finish="tool_calls")),
            sse(tool_event(arguments="x" * 4097), event(None, finish="tool_calls")),
        ):
            with self.subTest(payload=payload[:100]), self.assertRaises(ValueError):
                _ = [item async for item in bounded_sse(response(payload), allow_tools=True)]
