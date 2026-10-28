"""Validated, operator-owned settings; credentials are mounted separately."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neurwerk_pipecat.turn import temporary_credentials


class SecretRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str

    def read(self) -> str:
        value = Path(self.file).read_text().strip()
        if not value or len(value) > 16384:
            raise ValueError("Missing or oversized credential")
        return value


class Speech(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    key: SecretRef | None = None
    voice: str = "alloy"
    language: str = "en"


class OIDC(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issuer: str
    client_id: str = "pipecat"
    client_secret: SecretRef
    gateway_audience: str = "agentgateway"
    admission_role: str = "pipecat-user"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: str = Field(pattern=r"^[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?$")
    url_slug: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    sessions_enabled: bool = False
    title: str = Field(default="Voice assistant", max_length=100)
    public_origin: str
    anonymous: bool = False
    anonymous_key: SecretRef | None = None
    oidc: OIDC | None = None
    gateway_origin: str = ""
    gateway_base_url: str = ""
    binding_id: str = Field(default="", pattern=r"^$|^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    model_id: str = Field(default="", max_length=200)
    stt: Speech | None = None
    tts: Speech | None = None
    ice_servers: list[dict] = Field(default_factory=list, max_length=8)
    browser_ice_servers: list[dict] = Field(default_factory=list, max_length=8)
    browser_ice_secret: SecretRef | None = None
    server_ice_secret: SecretRef | None = None
    turn_secret: SecretRef | None = None
    turn_urls: list[str] = Field(default_factory=list, max_length=4)
    session_seconds: int = Field(default=1800, ge=60, le=14400)
    idle_seconds: int = Field(default=120, ge=30, le=600)
    max_connections: int = Field(default=4, ge=1, le=32)
    max_web_sessions: int = Field(default=128, ge=8, le=4096)
    max_history_chars: int = Field(default=32000, ge=1024, le=128000)
    script: str = "bundled"

    @model_validator(mode="after")
    def validate_boundaries(self) -> "Settings":
        if self.bot_id in {"turn", "product-values"}:
            raise ValueError("Bot ID is reserved for managed infrastructure")
        if self.sessions_enabled and not all(
            (
                self.gateway_base_url or (self.gateway_origin and self.binding_id),
                self.model_id,
                self.stt,
                self.tts,
            )
        ):
            raise ValueError("Enabled sessions require all gateway and speech settings")
        for value in [self.public_origin] + ([self.gateway_origin] if self.gateway_origin else []):
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Public and gateway origins must use HTTPS without credentials")
            if url.path not in ("", "/") or url.query or url.fragment:
                raise ValueError("Expected an origin, not an endpoint path")
        if self.gateway_base_url:
            url = urlsplit(self.gateway_base_url)
            path = url.path.rstrip("/")
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or not path.endswith("/v1")
                or path == "/v1"
            ):
                raise ValueError("Expected a bound gateway HTTPS API base URL")
        for speech in (self.stt, self.tts):
            if speech is None:
                continue
            url = urlsplit(speech.base_url)
            if url.scheme not in ("https", "http") or not url.hostname or url.username:
                raise ValueError("Invalid speech API URL")
        if self.anonymous:
            if self.sessions_enabled and not self.anonymous_key:
                raise ValueError("Anonymous bots require their own key")
        elif not self.oidc or self.anonymous_key:
            raise ValueError("Protected bots require OIDC and forbid anonymous credentials")
        if self.oidc:
            if not re.fullmatch(r"https://[^?#]+", self.oidc.issuer):
                raise ValueError("OIDC issuer must use HTTPS")
        return self

    @property
    def base_path(self) -> str:
        return f"/{self.url_slug}"

    @property
    def callback(self) -> str:
        return f"{self.public_origin.rstrip('/')}{self.base_path}/auth/callback"

    @property
    def gateway_url(self) -> str:
        if self.gateway_base_url:
            return self.gateway_base_url.rstrip("/") + "/chat/completions"
        return f"{self.gateway_origin.rstrip('/')}/bindings/{self.binding_id}/v1/chat/completions"


def load_settings() -> Settings:
    try:
        return Settings.model_validate_json(Path(os.environ["PIPECAT_CONFIG"]).read_text())
    except (ValueError, OSError, KeyError):
        raise SystemExit("Invalid or unreadable runtime configuration") from None


def browser_ice(settings: Settings) -> list[dict]:
    if settings.browser_ice_secret:
        value = json.loads(settings.browser_ice_secret.read())
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError("Invalid browser ICE server list")
        return value + managed_turn(settings)
    return settings.browser_ice_servers + managed_turn(settings)


def server_ice(settings: Settings) -> list[dict]:
    if settings.server_ice_secret:
        value = json.loads(settings.server_ice_secret.read())
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError("Invalid server ICE server list")
        return value + managed_turn(settings)
    return settings.ice_servers + managed_turn(settings)


def managed_turn(settings: Settings) -> list[dict]:
    if not settings.turn_secret:
        return []
    return [
        {
            "urls": settings.turn_urls,
            **temporary_credentials(
                settings.turn_secret.read(), settings.session_seconds + 60, settings.bot_id
            ),
        }
    ]
