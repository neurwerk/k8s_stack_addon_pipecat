"""One process per bot, authenticated signaling and bounded connection lifetimes."""

import asyncio
import contextlib
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from aiortc.sdp import candidate_from_sdp
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer
from pydantic import BaseModel, ConfigDict, Field

from simple_chatbot.auth import Auth, WebSession
from simple_chatbot.config import Settings, browser_ice, load_settings, server_ice
from simple_chatbot.webrtc import BrowserWebRTCConnection as SmallWebRTCConnection


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sdp: str = Field(min_length=1, max_length=131072)
    type: Literal["offer"]
    pc_id: str | None = Field(default=None, max_length=128)
    restart_pc: bool = False
    request_data: dict | None = None
    requestData: dict | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: str = Field(max_length=4096)
    sdp_mid: str = Field(max_length=32)
    sdp_mline_index: int = Field(ge=0, le=16)


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pc_id: str = Field(max_length=128)
    candidates: list[Candidate] = Field(max_length=32)


@dataclass
class Peer:
    owner: str
    connection: SmallWebRTCConnection | WebSocket
    expires: float
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def create_app(settings: Settings) -> FastAPI:
    peers: dict[str, Peer] = {}
    notices: dict[str, list[dict]] = {}
    tickets: dict[str, tuple[str, float]] = {}
    create_lock = asyncio.Lock()

    async def close_peer(pc_id: str) -> None:
        peer = peers.pop(pc_id, None)
        if not peer:
            return
        if peer.task and peer.task is not asyncio.current_task():
            peer.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(peer.task, timeout=10)
        if isinstance(peer.connection, WebSocket):
            with contextlib.suppress(RuntimeError):
                await peer.connection.close(code=1000)
        else:
            await peer.connection.disconnect()

    async def reap() -> None:
        while True:
            await asyncio.sleep(5)
            auth.prune()
            for key, (owner, expiry) in list(tickets.items()):
                if expiry <= time.time() or owner not in auth.sessions:
                    tickets.pop(key, None)
            for pc_id, peer in list(peers.items()):
                if (
                    peer.owner not in auth.sessions
                    or time.time() >= peer.expires
                    or (peer.task and peer.task.done())
                ):
                    await close_peer(pc_id)
            for owner in list(notices):
                if owner not in auth.sessions:
                    notices.pop(owner, None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await auth.initialize()
            from simple_chatbot.bot import run_bot
        except BaseException:
            await http.aclose()
            raise
        app.state.run_bot = run_bot
        reaper = asyncio.create_task(reap())
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            await asyncio.gather(*(close_peer(key) for key in list(peers)))
            await http.aclose()

    http = httpx.AsyncClient(
        timeout=httpx.Timeout(45, connect=10),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        trust_env=False,
    )
    auth = Auth(settings, http)
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    base = settings.base_path

    async def bounded_request(request: Request, call_next):
        # Bound chunked bodies too, before FastAPI attempts JSON parsing.
        length = request.headers.get("content-length")
        if length and (not length.isdecimal() or int(length) > 196608):
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method in ("POST", "PATCH", "DELETE"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 196608:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' blob:; worker-src 'self' blob:; "
            "style-src 'self'; style-src-attr 'unsafe-inline'; "
            "connect-src 'self'; media-src 'self' blob:; img-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self)"
        return response

    active_requests = 0

    @app.middleware("http")
    async def limits(request: Request, call_next):
        nonlocal active_requests
        if active_requests >= 64:
            return JSONResponse({"detail": "Request limit reached"}, status_code=429)
        active_requests += 1
        try:
            async with asyncio.timeout(30):
                return await bounded_request(request, call_next)
        except TimeoutError:
            return JSONResponse({"detail": "Request timed out"}, status_code=504)
        except Exception:
            logger.error("HTTP operation failed; payload omitted")
            return JSONResponse({"detail": "Operation failed"}, status_code=500)
        finally:
            active_requests -= 1

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get(base + "/api/config")
    async def config(request: Request):
        response = JSONResponse({})
        session: WebSession | None = None
        try:
            session = auth.get(request)
        except HTTPException:
            if settings.anonymous:
                session = auth.create(
                    settings.anonymous_key.read() if settings.anonymous_key else "",
                    "anonymous",
                    time.time() + settings.session_seconds,
                )
                auth.set_cookie(response, session)
        result: dict = {
            "title": settings.title,
            "anonymous": settings.anonymous,
            "authenticated": session is not None,
            "signedIn": session.signed_in if session else False,
            "loginAvailable": settings.oidc is not None,
            "sessionsEnabled": settings.sessions_enabled,
            "unavailableReason": ""
            if settings.sessions_enabled
            else "Voice sessions are not configured. Sign-in and the interface remain available.",
        }
        if session:
            result.update(
                {
                    "csrf": session.csrf,
                    "iceServers": browser_ice(settings) if settings.sessions_enabled else [],
                    "expires": session.expires,
                }
            )
        response.body = JSONResponse(result).body
        response.headers["content-length"] = str(len(response.body))
        return response

    @app.get(base + "/auth/login")
    async def login():
        return auth.login()

    @app.get(base + "/auth/callback")
    async def callback(request: Request):
        return await auth.callback(request)

    @app.get(base + "/auth/signed-out")
    async def signed_out():
        if settings.anonymous:
            return RedirectResponse(base + "/", status_code=303)
        return HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>Signed out</title></head><body><main><h1>You are signed out</h1>"
            "<p>You have signed out of this app. Your Keycloak session may still be active.</p>"
            f'<a href="{base}/auth/login">Sign in again</a></main></body></html>'
        )

    @app.post(base + "/auth/logout")
    async def logout(request: Request):
        session = auth.mutation(request)
        auth.sessions.pop(session.id, None)
        await asyncio.gather(
            *(close_peer(key) for key, peer in list(peers.items()) if peer.owner == session.id)
        )
        notices.pop(session.id, None)
        response = JSONResponse({"status": "signed-out"})
        response.delete_cookie(auth.cookie, path=base)
        return response

    def owned(pc_id: str, session: WebSession) -> Peer:
        peer = peers.get(pc_id)
        if not peer or peer.owner != session.id or peer.expires <= time.time():
            raise HTTPException(404, "Connection not found")
        return peer

    def gateway_credential(session: WebSession) -> str:
        # Optional public-bot login changes identity, never the configured billing mode.
        if settings.anonymous:
            assert settings.anonymous_key
            return settings.anonymous_key.read()
        return session.token

    @app.post(base + "/api/offer")
    async def offer(body: Offer, request: Request):
        session = auth.mutation(request)
        if not settings.sessions_enabled:
            raise HTTPException(409, "Voice sessions are not configured")
        if body.pc_id:
            peer = owned(body.pc_id, session)
            if isinstance(peer.connection, WebSocket):
                raise HTTPException(404, "Not a WebRTC connection")
            async with peer.lock:
                async with asyncio.timeout(20):
                    await peer.connection.renegotiate(body.sdp, body.type, body.restart_pc)
                return peer.connection.get_answer()
        async with create_lock:
            if len(peers) >= settings.max_connections:
                raise HTTPException(429, "Bot connection limit reached")
            if any(peer.owner == session.id for peer in peers.values()):
                raise HTTPException(409, "This browser already has an active connection")
            connection = SmallWebRTCConnection(
                ice_servers=[IceServer(**server) for server in server_ice(settings)]
            )
            peer = Peer(session.id, connection, session.expires)
            try:
                async with asyncio.timeout(20):
                    await connection.initialize(body.sdp, body.type)
                answer = connection.get_answer()
                if not answer:
                    raise ValueError("Missing answer")
                peers[connection.pc_id] = peer

                def notice(text: str) -> None:
                    events = notices.setdefault(session.id, [])
                    events.append({"type": "pii-notice", "text": text})
                    del events[:-8]

                async def run() -> None:
                    try:
                        async with asyncio.timeout(max(0, session.expires - time.time())):
                            await app.state.run_bot(
                                connection, settings, http, gateway_credential(session), notice
                            )
                    except TimeoutError:
                        pass
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.error("Bot session failed; payload omitted")
                    finally:
                        await close_peer(connection.pc_id)

                peer.task = asyncio.create_task(run())
                return answer
            except BaseException:
                peers.pop(connection.pc_id, None)
                await connection.disconnect()
                raise

    @app.patch(base + "/api/offer")
    async def patch(body: Patch, request: Request):
        peer = owned(body.pc_id, auth.mutation(request))
        if isinstance(peer.connection, WebSocket):
            raise HTTPException(404, "Not a WebRTC connection")
        async with peer.lock:
            for item in body.candidates:
                candidate = candidate_from_sdp(item.candidate) if item.candidate else None
                if candidate:
                    candidate.sdpMid = item.sdp_mid
                    candidate.sdpMLineIndex = item.sdp_mline_index
                await peer.connection.add_ice_candidate(candidate)
        return {"status": "ok"}

    @app.delete(base + "/api/connections/{pc_id}")
    async def disconnect(pc_id: str, request: Request):
        owned(pc_id, auth.mutation(request))
        await close_peer(pc_id)
        return {"status": "closed"}

    @app.get(base + "/api/notices")
    async def events(request: Request):
        return {"notices": notices.pop(auth.get(request).id, [])}

    @app.post(base + "/api/cancel")
    async def cancel(request: Request):
        session = auth.mutation(request)
        async with create_lock:
            await asyncio.gather(
                *(close_peer(key) for key, peer in list(peers.items()) if peer.owner == session.id)
            )
        return {"status": "closed"}

    @app.post(base + "/api/websocket-ticket")
    async def websocket_ticket(request: Request):
        session = auth.mutation(request)
        if not settings.sessions_enabled:
            raise HTTPException(409, "Voice sessions are not configured")
        for key, (owner, expiry) in list(tickets.items()):
            if owner == session.id or expiry <= time.time():
                tickets.pop(key, None)
        if len(tickets) >= settings.max_web_sessions:
            raise HTTPException(429, "Ticket limit reached")
        ticket = secrets.token_urlsafe(32)
        tickets[ticket] = (session.id, time.time() + 20)
        return {"ticket": ticket}

    @app.websocket(base + "/api/websocket")
    async def websocket(websocket: WebSocket):
        if not settings.sessions_enabled:
            await websocket.close(code=1008)
            return
        try:
            session = auth.get(websocket)
        except HTTPException:
            await websocket.close(code=1008)
            return
        ticket = tickets.pop(websocket.query_params.get("ticket", ""), None)
        if (
            websocket.headers.get("origin") != settings.public_origin.rstrip("/")
            or not ticket
            or ticket[0] != session.id
            or ticket[1] <= time.time()
        ):
            await websocket.close(code=1008)
            return
        key = "ws-" + secrets.token_urlsafe(16)
        async with create_lock:
            if len(peers) >= settings.max_connections or any(
                peer.owner == session.id for peer in peers.values()
            ):
                await websocket.close(code=1013)
                return
            await websocket.accept()
            peers[key] = Peer(session.id, websocket, session.expires, task=asyncio.current_task())

        def notice(text: str) -> None:
            events = notices.setdefault(session.id, [])
            events.append({"type": "pii-notice", "text": text})
            del events[:-8]

        try:
            async with asyncio.timeout(max(0, session.expires - time.time())):
                await app.state.run_bot(
                    websocket, settings, http, gateway_credential(session), notice
                )
        except (TimeoutError, RuntimeError, WebSocketDisconnect):
            pass
        except Exception:
            logger.error("WebSocket session failed; payload omitted")
        finally:
            await close_peer(key)

    static = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if static.is_dir():
        app.mount(base + "/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get(base + "/")
        async def index(request: Request):
            if not settings.anonymous:
                try:
                    signed_in = auth.get(request).signed_in
                except HTTPException:
                    signed_in = False
                if not signed_in:
                    return RedirectResponse(base + "/auth/login", status_code=303)
            return FileResponse(static / "index.html")

        @app.get("/")
        async def root():
            return RedirectResponse(base + "/")

    return app


def main() -> None:
    import sys

    import uvicorn

    # Upstream debug output can include transcript text; never enable it in this runtime.
    logger.remove()
    logger.add(
        sys.stderr,
        level="ERROR",
        format="{time} {level} {message}",
        filter=lambda record: record["name"].startswith("simple_chatbot"),
    )
    uvicorn.run(
        create_app(load_settings()),
        host="0.0.0.0",
        port=7860,
        workers=1,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
        ws_max_size=65536,
        ws_max_queue=8,
        limit_concurrency=128,
    )
