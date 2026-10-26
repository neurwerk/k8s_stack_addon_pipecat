"""Bounded in-memory OIDC sessions. No tokens are stored in browser cookies."""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection
from starlette.responses import RedirectResponse, Response

from neurwerk_pipecat.config import Settings


@dataclass
class WebSession:
    id: str
    csrf: str
    expires: float
    token: str
    subject: str


@dataclass
class Login:
    verifier: str
    nonce: str
    expires: float


class Auth:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        self.sessions: dict[str, WebSession] = {}
        self.logins: dict[str, Login] = {}
        self.metadata: dict = {}
        self.jwks: dict = {}
        self.cookie = f"__Secure-pipecat-{settings.bot_id}"

    async def initialize(self) -> None:
        oidc = self.settings.oidc
        if not oidc:
            return
        discovery = oidc.issuer.rstrip("/") + "/.well-known/openid-configuration"
        response = await self.http.get(discovery)
        response.raise_for_status()
        self.metadata = response.json()
        if self.metadata.get("issuer") != oidc.issuer:
            raise ValueError("OIDC discovery issuer mismatch")
        # Do not follow discovery to non-TLS endpoints or URLs with embedded credentials.
        from urllib.parse import urlsplit

        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            url = urlsplit(self.metadata[key])
            if url.scheme != "https" or not url.hostname or url.username or url.fragment:
                raise ValueError("Unsafe OIDC discovery endpoint")
        await self.refresh_jwks()

    async def refresh_jwks(self) -> None:
        response = await self.http.get(self.metadata["jwks_uri"])
        response.raise_for_status()
        self.jwks = response.json()

    def prune(self) -> None:
        now = time.time()
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires > now}
        self.logins = {key: value for key, value in self.logins.items() if value.expires > now}

    def create(self, token: str, subject: str, expires: float) -> WebSession:
        self.prune()
        if len(self.sessions) >= self.settings.max_web_sessions:
            raise HTTPException(429, "Session limit reached")
        session = WebSession(
            secrets.token_urlsafe(32), secrets.token_urlsafe(32), expires, token, subject
        )
        self.sessions[session.id] = session
        return session

    def set_cookie(self, response: Response, session: WebSession) -> None:
        response.set_cookie(
            self.cookie,
            session.id,
            secure=True,
            httponly=True,
            samesite="lax",
            path=self.settings.base_path,
            max_age=max(0, int(session.expires - time.time())),
        )

    def get(self, request: HTTPConnection) -> WebSession:
        self.prune()
        session = self.sessions.get(request.cookies.get(self.cookie, ""))
        if not session:
            raise HTTPException(401, "Sign in or reload to start a session")
        return session

    def mutation(self, request: Request) -> WebSession:
        session = self.get(request)
        if request.headers.get("origin") != self.settings.public_origin.rstrip("/"):
            raise HTTPException(403, "Origin rejected")
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
            raise HTTPException(403, "CSRF check failed")
        return session

    def login(self) -> RedirectResponse:
        if not self.settings.oidc:
            raise HTTPException(404)
        self.prune()
        if len(self.logins) >= self.settings.max_web_sessions:
            raise HTTPException(429, "Login limit reached")
        state = secrets.token_urlsafe(32)
        login = Login(secrets.token_urlsafe(48), secrets.token_urlsafe(32), time.time() + 300)
        self.logins[state] = login
        challenge = base64.urlsafe_b64encode(hashlib.sha256(login.verifier.encode()).digest())
        query = urlencode(
            {
                "client_id": self.settings.oidc.client_id,
                "response_type": "code",
                "redirect_uri": self.settings.callback,
                "scope": "openid profile",
                "state": state,
                "nonce": login.nonce,
                "code_challenge": challenge.decode().rstrip("="),
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(self.metadata["authorization_endpoint"] + "?" + query)
        response.set_cookie(
            self.cookie + "-login",
            state,
            max_age=300,
            secure=True,
            httponly=True,
            samesite="lax",
            path=self.settings.base_path + "/auth",
        )
        return response

    async def decode(self, token: str, audience: str) -> dict:
        oidc = self.settings.oidc
        assert oidc
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ValueError("Unsupported signing key")
        keys = [k for k in self.jwks["keys"] if k.get("kid") == header["kid"]]
        if not keys:
            await self.refresh_jwks()
            keys = [k for k in self.jwks["keys"] if k.get("kid") == header["kid"]]
        if len(keys) != 1:
            raise ValueError("Unknown signing key")
        return jwt.decode(
            token,
            jwt.PyJWK.from_dict(keys[0]).key,
            algorithms=["RS256"],
            audience=audience,
            issuer=oidc.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )

    async def callback(self, request: Request) -> Response:
        self.prune()
        state = request.query_params.get("state", "")
        cookie = request.cookies.get(self.cookie + "-login", "")
        if not state or not secrets.compare_digest(state, cookie):
            raise HTTPException(400, "Invalid login state")
        login = self.logins.pop(state, None)
        code = request.query_params.get("code", "")
        if not login or not code or len(code) > 8192:
            raise HTTPException(400, "Expired or invalid login")
        oidc = self.settings.oidc
        assert oidc
        try:
            result = await self.http.post(
                self.metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.callback,
                    "client_id": oidc.client_id,
                    "client_secret": oidc.client_secret.read(),
                    "code_verifier": login.verifier,
                },
            )
            result.raise_for_status()
            tokens = result.json()
            identity = await self.decode(tokens["id_token"], oidc.client_id)
            access = await self.decode(tokens["access_token"], oidc.gateway_audience)
            if identity.get("nonce") != login.nonce or identity["sub"] != access["sub"]:
                raise ValueError("Login identity mismatch")
            if access.get("azp") != oidc.client_id:
                raise ValueError("Wrong authorized client")
            roles = access.get("realm_access", {}).get("roles", [])
            if oidc.admission_role not in roles:
                raise ValueError("Application admission denied")
            expiry = min(float(access["exp"]), time.time() + self.settings.session_seconds)
            session = self.create(tokens["access_token"], access["sub"], expiry)
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise HTTPException(403, "Login validation failed") from None
        old = request.cookies.get(self.cookie, "")
        self.sessions.pop(old, None)
        response = RedirectResponse(self.settings.base_path + "/", status_code=303)
        self.set_cookie(response, session)
        response.delete_cookie(self.cookie + "-login", path=self.settings.base_path + "/auth")
        return response
