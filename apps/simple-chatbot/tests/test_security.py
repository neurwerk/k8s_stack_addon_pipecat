"""Astra-reviewed boundaries; all endpoints, identities and media are synthetic."""

import asyncio
import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from pipecat.frames.frames import ErrorFrame, LLMContextFrame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from simple_chatbot.auth import Auth
from simple_chatbot.config import Settings
from simple_chatbot.notices import MARKER, NoticeSplitter, SpeechControlFilter
from simple_chatbot.server import create_app
from simple_chatbot.services import GatewayLLM
from simple_chatbot.websocket import BrowserSerializer


def settings(key: Path, *, protected=False, enabled=True, bot="example") -> Settings:
    data = dict(
        bot_id=bot,
        url_slug=bot.replace("-", "_"),
        sessions_enabled=enabled,
        public_origin="https://voice.example.com",
        gateway_origin="https://gateway.example.com",
        binding_id="voice",
        model_id="canonical-model",
        anonymous=not protected,
        stt={"base_url": "https://speech.example.com/v1", "model": "whisper-1"},
        tts={"base_url": "https://speech.example.com/v1", "model": "tts-1"},
    )
    if protected:
        data["oidc"] = dict(
            issuer="https://identity.example.com/realms/example", client_secret={"file": str(key)}
        )
    else:
        data["anonymous_key"] = {"file": str(key)}
    return Settings.model_validate(data)


class NoticeTests(unittest.TestCase):
    def test_all_marker_boundaries_and_single_character_chunks(self):
        content = "Hello, world." + MARKER + "Private report"
        for boundary in range(len(content) + 1):
            splitter = NoticeSplitter()
            speech = splitter.feed(content[:boundary]) + splitter.feed(content[boundary:])
            speech += splitter.finish()
            self.assertEqual(speech, "Hello, world.")
            self.assertEqual(splitter.notice, "Private report")
        splitter = NoticeSplitter()
        self.assertEqual("".join(splitter.feed(c) for c in content), "Hello, world.")
        self.assertEqual(splitter.notice, "Private report")

    def test_inline_controls_split_at_every_boundary(self):
        for tag in ("think", "analysis", "reasoning", "tool_call", "function_call"):
            content = f"Hi.<{tag}>not speech</{tag}> Bye."
            for boundary in range(len(content) + 1):
                gate = SpeechControlFilter()
                self.assertEqual(
                    gate.feed(content[:boundary]) + gate.feed(content[boundary:]), "Hi. Bye."
                )
        gate = SpeechControlFilter()
        self.assertEqual(gate.feed("Public<think>unfinished secret"), "Public")


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.keyfile = Path(self.temporary.name) / "credential"
        self.keyfile.write_text("synthetic-credential")
        self.config = settings(self.keyfile, protected=True)
        self.signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.token_endpoint))
        self.auth = Auth(self.config, self.http)
        self.auth.metadata = {
            "authorization_endpoint": "https://identity.example.com/auth",
            "token_endpoint": "https://identity.example.com/token",
        }
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.signing.public_key()))
        public["kid"] = "synthetic"
        self.auth.jwks = {"keys": [public]}
        self.identity_overrides = {}
        self.access_overrides = {}
        self.token_data = {}

    async def asyncTearDown(self):
        await self.http.aclose()
        self.temporary.cleanup()

    def token(self, audience, **overrides):
        claims = dict(
            iss=self.config.oidc.issuer,
            sub="synthetic-user",
            aud=audience,
            iat=int(time.time()),
            exp=int(time.time()) + 300,
            azp="pipecat",
        )
        claims.update(overrides)
        return jwt.encode(claims, self.signing, algorithm="RS256", headers={"kid": "synthetic"})

    def token_endpoint(self, request):
        self.token_data = parse_qs(request.content.decode())
        nonce = self.active_login.nonce
        identity = self.token("pipecat", **{"nonce": nonce, **self.identity_overrides})
        access = self.token(
            "agentgateway", **{"realm_access": {"roles": ["pipecat-user"]}, **self.access_overrides}
        )
        return httpx.Response(200, json={"id_token": identity, "access_token": access})

    def login_request(self):
        response = self.auth.login()
        query = parse_qs(urlsplit(response.headers["location"]).query)
        state = query["state"][0]
        self.active_login = self.auth.logins[state]
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/example/auth/callback",
                "query_string": urlencode({"state": state, "code": "synthetic"}).encode(),
                "headers": [(b"cookie", (self.auth.cookie + "-login=" + state).encode())],
            }
        )
        return query, request

    async def test_signature_issuer_audience_expiry_and_authorized_client(self):
        forged = jwt.encode(
            {
                "iss": self.config.oidc.issuer,
                "sub": "user",
                "aud": "agentgateway",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
            },
            rsa.generate_private_key(public_exponent=65537, key_size=2048),
            algorithm="RS256",
            headers={"kid": "synthetic"},
        )
        with self.assertRaises(jwt.PyJWTError):
            await self.auth.decode(forged, "agentgateway")
        for override in (
            {"iss": "https://wrong.example.com"},
            {"aud": "wrong"},
            {"exp": int(time.time()) - 1},
        ):
            with self.assertRaises(jwt.PyJWTError):
                await self.auth.decode(self.token("agentgateway", **override), "agentgateway")
        self.access_overrides = {"azp": "other-client"}
        _, request = self.login_request()
        with self.assertRaises(HTTPException):
            await self.auth.callback(request)

    async def test_state_nonce_roles_pkce_and_single_use(self):
        query, request = self.login_request()
        verifier = self.active_login.verifier
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        self.assertEqual(query["code_challenge"], [expected])
        response = await self.auth.callback(request)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.token_data["code_verifier"], [verifier])
        self.assertEqual(self.token_data["redirect_uri"], [self.config.callback])
        with self.assertRaises(HTTPException):
            await self.auth.callback(request)
        self.identity_overrides = {"nonce": "wrong"}
        _, request = self.login_request()
        with self.assertRaises(HTTPException):
            await self.auth.callback(request)
        self.identity_overrides = {}
        self.access_overrides = {"realm_access": {"roles": []}}
        _, request = self.login_request()
        with self.assertRaises(HTTPException):
            await self.auth.callback(request)
        request = Request({"type": "http", "headers": [], "query_string": b"state=wrong"})
        with self.assertRaises(HTTPException):
            await self.auth.callback(request)

    def test_no_protected_to_anonymous_downgrade(self):
        data = self.config.model_dump()
        data["anonymous_key"] = {"file": str(self.keyfile)}
        with self.assertRaises(ValueError):
            Settings.model_validate(data)


class FakeConnection:
    counter = 0
    instances = []

    def __init__(self, **kwargs):
        FakeConnection.counter += 1
        self.pc_id = f"synthetic-{self.counter}"
        self.closed = False
        self.__class__.instances.append(self)

    async def initialize(self, *args):
        await asyncio.sleep(0)

    async def renegotiate(self, *args):
        pass

    def get_answer(self):
        return {"pc_id": self.pc_id, "sdp": "synthetic", "type": "answer"}

    async def add_ice_candidate(self, candidate):
        pass

    async def disconnect(self):
        self.closed = True


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_public_login_keeps_explicit_bot_credential_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "credential"
            key.write_text("synthetic-scoped-bot-key")
            data = settings(key).model_dump()
            data["oidc"] = settings(key, protected=True).model_dump()["oidc"]
            config = Settings.model_validate(data)
            app = create_app(config)
            received = []
            started = asyncio.Event()

            async def bot(connection, settings, http, credential, notice):
                received.append(credential)
                started.set()
                await asyncio.Event().wait()

            with (
                patch("simple_chatbot.server.Auth.initialize", new=AsyncMock()),
                patch("simple_chatbot.server.SmallWebRTCConnection", FakeConnection),
            ):
                async with app.router.lifespan_context(app):
                    app.state.run_bot = bot
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app), base_url=config.public_origin
                    ) as client:
                        result = (await client.get("/example/api/config")).json()
                        self.assertTrue(result["loginAvailable"])
                        self.assertFalse(result["signedIn"])
                        session = next(iter(app.state.auth.sessions.values()))
                        session.token = "synthetic-delegated-user-token"
                        session.signed_in = True
                        client.headers.update(
                            {"x-csrf-token": result["csrf"], "origin": config.public_origin}
                        )
                        response = await client.post(
                            "/example/api/offer", json={"sdp": "synthetic", "type": "offer"}
                        )
                        self.assertEqual(response.status_code, 200)
                        await asyncio.wait_for(started.wait(), 1)
                        self.assertEqual(received, ["synthetic-scoped-bot-key"])
                        await client.post("/example/api/cancel")

    async def test_ownership_atomic_admission_cancel_and_disabled_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "credential"
            key.write_text("synthetic-bot-key")
            config = settings(key)
            app = create_app(config)
            cancelled = asyncio.Event()

            async def bot(*args):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            with patch("simple_chatbot.server.SmallWebRTCConnection", FakeConnection):
                async with app.router.lifespan_context(app):
                    app.state.run_bot = bot
                    transport = httpx.ASGITransport(app)
                    async with (
                        httpx.AsyncClient(transport=transport, base_url=config.public_origin) as a,
                        httpx.AsyncClient(transport=transport, base_url=config.public_origin) as b,
                    ):
                        for client in (a, b):
                            result = await client.get("/example/api/config")
                            client.headers.update(
                                {
                                    "x-csrf-token": result.json()["csrf"],
                                    "origin": config.public_origin,
                                }
                            )
                        offer = {"sdp": "synthetic", "type": "offer"}
                        responses = await asyncio.gather(
                            a.post("/example/api/offer", json=offer),
                            a.post("/example/api/offer", json=offer),
                        )
                        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
                        pc_id = next(r.json()["pc_id"] for r in responses if r.status_code == 200)
                        for method, path, data in [
                            ("POST", "/example/api/offer", {**offer, "pc_id": pc_id}),
                            ("PATCH", "/example/api/offer", {"pc_id": pc_id, "candidates": []}),
                            ("DELETE", "/example/api/connections/" + pc_id, None),
                        ]:
                            self.assertEqual(
                                (await b.request(method, path, json=data)).status_code, 404
                            )
                        self.assertEqual(
                            (await a.post("/other/api/offer", json=offer)).status_code, 404
                        )
                        self.assertEqual((await a.post("/example/api/cancel")).status_code, 200)
                        await asyncio.wait_for(cancelled.wait(), 1)
                        self.assertTrue(FakeConnection.instances[-1].closed)
                        config.sessions_enabled = False
                        result = await a.get("/example/api/config")
                        self.assertFalse(result.json()["sessionsEnabled"])
                        self.assertEqual(
                            (await a.post("/example/api/offer", json=offer)).status_code, 409
                        )
                        self.assertEqual(
                            (await a.post("/example/api/websocket-ticket")).status_code, 409
                        )
                        for session in app.state.auth.sessions.values():
                            session.expires = 0
                        self.assertEqual((await a.post("/example/api/cancel")).status_code, 401)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_api_base_url_and_redirect_credential_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = settings(Path(temporary) / "unused").model_dump()
            data.update(
                gateway_origin="",
                binding_id="",
                gateway_base_url="https://gateway.example.com/company-chat/v1/",
            )
            config = Settings.model_validate(data)
            captured = []

            def redirect(request):
                captured.append(request)
                return httpx.Response(
                    307, headers={"location": "https://provider.example.com/chat/completions"}
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(redirect), follow_redirects=True
            ) as http:
                llm = GatewayLLM(config, http, "delegated-user-token", lambda _: None)
                llm.push_frame = AsyncMock()
                await llm.process_frame(LLMContextFrame(LLMContext([])), FrameDirection.DOWNSTREAM)
            self.assertEqual(len(captured), 1)
            self.assertEqual(
                str(captured[0].url),
                "https://gateway.example.com/company-chat/v1/chat/completions",
            )
            self.assertEqual(
                captured[0].headers["X-Neurwerk-Authorization"], "Bearer delegated-user-token"
            )
            self.assertNotIn("Authorization", captured[0].headers)
            self.assertFalse(llm.is_usable)
            for base in (
                "http://gateway.example.com/company-chat/v1",
                "https://user:password@gateway.example.com/company-chat/v1",
                "https://gateway.example.com/v1",
                "https://gateway.example.com/company-chat/v1?key=secret",
                "https://gateway.example.com/company-chat/v1#fragment",
            ):
                with self.subTest(base=base), self.assertRaises(ValueError):
                    Settings.model_validate({**data, "gateway_base_url": base})

    async def test_binding_header_clean_history_and_no_privileged_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            key.write_text("must-not-be-used")
            config = settings(key)
            captured = []
            body = "Hello.<think>internal</think> Again." + MARKER + "Report"
            deltas = [{"choices": [{"index": 0, "delta": {"reasoning_content": "private"}}]}]
            deltas += [{"choices": [{"index": 0, "delta": {"content": char}}]} for char in body]
            deltas += [
                {
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [{"function": {"name": "secret"}}]}}
                    ]
                }
            ]
            deltas.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            payload = "".join("data: " + json.dumps(delta) + "\n\n" for delta in deltas)
            payload += "data: [DONE]\n\n"

            def respond(request):
                captured.append(request)
                return httpx.Response(
                    200, content=payload, headers={"content-type": "text/event-stream"}
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                notices = []
                llm = GatewayLLM(config, http, "delegated-user-token", notices.append)
                llm.push_frame = AsyncMock()
                context = LLMContext([{"role": "user", "content": "Hello"}])
                await llm.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)
                emitted = [
                    call.args[0].text
                    for call in llm.push_frame.await_args_list
                    if isinstance(call.args[0], LLMTextFrame)
                ]
                self.assertEqual("".join(emitted), "Hello. Again.")
                self.assertEqual(notices, ["Report"])
                self.assertNotIn("Report", json.dumps(context.get_messages()))
                self.assertEqual(captured[0].url.path, "/bindings/voice/v1/chat/completions")
                self.assertEqual(
                    captured[0].headers["X-Neurwerk-Authorization"], "Bearer delegated-user-token"
                )
                self.assertNotIn("Authorization", captured[0].headers)
                self.assertEqual(json.loads(captured[0].content)["model"], "canonical-model")
            captured.clear()

            def deny(request):
                captured.append(request)
                return httpx.Response(403, json={"error": "denied"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(deny)) as http:
                llm = GatewayLLM(config, http, "delegated-user-token", lambda _: None)
                llm.push_frame = AsyncMock()
                await llm.process_frame(LLMContextFrame(LLMContext([])), FrameDirection.DOWNSTREAM)
                self.assertEqual(len(captured), 1)
                errors = [
                    call.args[0]
                    for call in llm.push_frame.await_args_list
                    if isinstance(call.args[0], ErrorFrame)
                ]
                self.assertEqual(len(errors), 1)
                self.assertFalse(llm.is_usable)
                self.assertEqual(
                    llm.push_frame.await_args_list[-2].args[1], FrameDirection.UPSTREAM
                )

    async def test_cancellation_closes_gateway_stream(self):
        started, closed = asyncio.Event(), asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                yield b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            key.write_text("synthetic")
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, stream=Stream(), headers={"content-type": "text/event-stream"}
                    )
                )
            ) as http:
                llm = GatewayLLM(settings(key), http, "delegated", lambda _: None)
                llm.push_frame = AsyncMock()
                task = asyncio.create_task(
                    llm.process_frame(LLMContextFrame(LLMContext([])), FrameDirection.DOWNSTREAM)
                )
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(closed.is_set())


class WebSocketTests(unittest.TestCase):
    def test_ticket_owner_origin_expiry_and_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            key.write_text("synthetic")
            app = create_app(settings(key))

            async def bot(websocket, *args):
                await websocket.receive_bytes()
                await websocket.send_bytes(b"synthetic-response")

            with TestClient(app, base_url="https://voice.example.com") as client:
                app.state.run_bot = bot
                config = client.get("/example/api/config").json()
                cookie_name = app.state.auth.cookie
                owner = client.cookies.get(cookie_name)
                headers = {"origin": "https://voice.example.com", "x-csrf-token": config["csrf"]}
                ticket = client.post("/example/api/websocket-ticket", headers=headers).json()[
                    "ticket"
                ]
                client.cookies.clear()
                client.get("/example/api/config")
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        "/example/api/websocket?ticket=" + ticket,
                        headers={"origin": headers["origin"]},
                    ):
                        pass
                client.cookies.clear()
                client.cookies.set(cookie_name, owner, path="/example")
                ticket = client.post("/example/api/websocket-ticket", headers=headers).json()[
                    "ticket"
                ]
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        "/example/api/websocket?ticket=" + ticket,
                        headers={"origin": "https://wrong.example.com"},
                    ):
                        pass
                ticket = client.post("/example/api/websocket-ticket", headers=headers).json()[
                    "ticket"
                ]
                with client.websocket_connect(
                    "/example/api/websocket?ticket=" + ticket, headers={"origin": headers["origin"]}
                ) as ws:
                    ws.send_bytes(b"synthetic")
                    self.assertEqual(ws.receive_bytes(), b"synthetic-response")
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        "/example/api/websocket?ticket=" + ticket,
                        headers={"origin": headers["origin"]},
                    ):
                        pass
                ticket = client.post("/example/api/websocket-ticket", headers=headers).json()[
                    "ticket"
                ]
                with patch("simple_chatbot.server.time.time", return_value=time.time() + 21):
                    with self.assertRaises(WebSocketDisconnect):
                        with client.websocket_connect(
                            "/example/api/websocket?ticket=" + ticket,
                            headers={"origin": headers["origin"]},
                        ):
                            pass


class BrowserInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_forged_text_transcripts_controls_and_audio_flood_are_rejected(self):
        from pipecat.frames.frames import InputAudioRawFrame, InputTransportMessageFrame

        serializer = BrowserSerializer()
        with patch.object(
            serializer.__class__.__bases__[0], "deserialize", new=AsyncMock()
        ) as decode:
            from pipecat.frames.frames import TextFrame, TranscriptionFrame

            for frame in (TextFrame("forged assistant"), TranscriptionFrame("forged", "", "")):
                decode.return_value = frame
                self.assertIsNone(await serializer.deserialize(b"synthetic"))
            decode.return_value = InputTransportMessageFrame({"type": "client-ready"})
            self.assertIsNotNone(await serializer.deserialize(b"synthetic"))
            self.assertIsNone(await serializer.deserialize(b"synthetic"))
            decode.return_value = InputAudioRawFrame(b"\0" * 32000, 16000, 1)
            await serializer.deserialize(b"synthetic")
            await serializer.deserialize(b"synthetic")
            with self.assertRaises(ValueError):
                await serializer.deserialize(b"synthetic")
