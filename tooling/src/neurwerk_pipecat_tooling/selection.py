# Copyright (c) 2026 neurwerk
# SPDX-License-Identifier: MIT
# Adapted from shared Tooling commit 4565779e7dacbf43c6f3f0868fc4c491d02b415b.
"""Validate Pipecat settings without network access, credentials or custody actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

_ID = re.compile(r"[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?")
_SLUG = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_PROVIDER_GRANT = re.compile(r"provider:[a-z0-9][a-z0-9-]{0,47}:company-use")
_PREFIX = "addons/pipecat"
_BRIDGE = "auth-keycloak-api-key-bridge"
_RESERVED_IDS = {"turn", "product-values"}


class SelectionError(ValueError):
    """Invalid Pipecat configuration; supplied values are never included in errors."""


@dataclass(frozen=True)
class PipecatBot:
    """Identify selected credentials, independently of voice-session availability."""

    id: str
    gateway: bool
    speech: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not _ID.fullmatch(self.id)
            or self.id in _RESERVED_IDS
            or any(field not in ("sttApiKey", "ttsApiKey") for field in self.speech)
        ):
            raise SelectionError("Invalid Pipecat bot credential selection")


@dataclass(frozen=True)
class PipecatSelection:
    """Keep active features separate from managed credential ownership."""

    oidc: bool
    turn: bool
    bots: tuple[PipecatBot, ...]
    managed: bool

    def paths(self, namespace: str) -> tuple[str, ...]:
        if not self.managed:
            return ()
        if namespace == "auth-keycloak":
            return ((f"{_PREFIX}/oidc",) if self.oidc else ()) + tuple(
                f"{_PREFIX}/machines/{bot.id}" for bot in self.bots if bot.gateway
            )
        if namespace == _BRIDGE:
            return tuple(f"{_PREFIX}/verifiers/{bot.id}" for bot in self.bots if bot.gateway)
        if namespace == "pipecat":
            return (
                ((f"{_PREFIX}/oidc",) if self.oidc else ())
                + ((f"{_PREFIX}/turn",) if self.turn else ())
                + tuple(
                    f"{_PREFIX}/bots/{bot.id}" for bot in self.bots if bot.gateway or bot.speech
                )
            )
        raise SelectionError("Invalid Pipecat delivery namespace")

    def refresh_targets(self) -> RefreshTargets:
        """Resolve only compiled targets; never accept workload names from user values."""
        secrets: list[tuple[str, str]] = []
        if self.oidc and self.managed:
            secrets.extend(
                (namespace, "pipecat-oidc-secret") for namespace in ("auth-keycloak", "pipecat")
            )
        if self.turn and self.managed:
            secrets.append(("pipecat", "pipecat-turn-secret"))
        for bot in self.bots:
            name = f"pipecat-bot-{bot.id}"
            if bot.gateway or bot.speech:
                secrets.append(("pipecat", name))
            if bot.gateway:
                secrets.extend(((_BRIDGE, f"{name}-verifier"), ("auth-keycloak", f"{name}-oidc")))
        consumers: list[tuple[str, str]] = []
        if self.oidc:
            consumers.extend(
                ("auth-keycloak", name)
                for name in (
                    "pipecat-oidc-settings",
                    "keycloak-pipecat-access",
                    "keycloak-pipecat-oidc",
                )
            )
        if any(bot.gateway for bot in self.bots):
            consumers.extend(
                (
                    ("auth-keycloak", "keycloak-pipecat-machines"),
                    (_BRIDGE, "pipecat-gateway-registrations"),
                )
            )
        consumers.append(("pipecat", "pipecat-shared"))
        deployments = [("pipecat", f"pipecat-{bot.id}") for bot in self.bots]
        if self.turn:
            deployments.append(("pipecat", "pipecat-turn"))
        return RefreshTargets(tuple(secrets), tuple(consumers), tuple(deployments))


@dataclass(frozen=True)
class RefreshTargets:
    """Domain resolution only; not the generic custody plan or an execution interface."""

    external_secrets: tuple[tuple[str, str], ...]
    helm_releases: tuple[tuple[str, str], ...]
    deployments: tuple[tuple[str, str], ...]


def select_pipecat_values(client_values: object, product_values: object) -> PipecatSelection:
    """Merge fixed shared/product inputs in the same order as the Helm releases."""
    if not isinstance(client_values, dict) or not isinstance(product_values, dict):
        raise SelectionError("Invalid Pipecat values")
    shared = _settings(client_values, "pipecat")
    overrides = _settings(product_values, "pipecat")
    combined = {**shared, **overrides}
    for feature in ("oidc", "turn", "secretSync"):
        defaults = _settings(shared, feature)
        settings = _settings(overrides, feature)
        merged = {**defaults, **settings}
        if feature == "secretSync":
            merged["stores"] = {**_settings(defaults, "stores"), **_settings(settings, "stores")}
        combined[feature] = merged
    return select_pipecat(combined)


def select_pipecat(product: object) -> PipecatSelection:
    if not isinstance(product, dict) or product.get("enabled") is not True:
        raise SelectionError("Pipecat must be explicitly enabled")
    sync = _settings(product, "secretSync")
    managed = _enabled(sync, False)
    stores = _settings(sync, "stores")
    if managed and any(
        stores.get(namespace, f"{namespace}-openbao-secret-store")
        != f"{namespace}-openbao-secret-store"
        for namespace in ("pipecat", "auth-keycloak", _BRIDGE)
    ):
        raise SelectionError("Pipecat delivery requires the reviewed namespace-local SecretStores")
    rows = product.get("bots", [])
    if not isinstance(rows, list) or len(rows) > 32:
        raise SelectionError("Invalid Pipecat bot catalog")
    identities: set[str] = set()
    slugs: set[str] = set()
    bots: list[PipecatBot] = []
    protected = False
    for row in rows:
        if not isinstance(row, dict):
            raise SelectionError("Invalid Pipecat bot catalog")
        identity = row.get("id")
        slug = row.get("urlSlug", identity)
        if (
            not isinstance(identity, str)
            or not _ID.fullmatch(identity)
            or identity in identities
            or identity in _RESERVED_IDS
            or not isinstance(slug, str)
            or not _SLUG.fullmatch(slug)
            or slug in slugs
            or slug in ("healthz", "assets")
            or type(row.get("anonymous")) is not bool
            or type(row.get("sessionsEnabled", False)) is not bool
        ):
            raise SelectionError("Invalid or duplicate Pipecat bot ID or URL slug")
        identities.add(identity)
        slugs.add(slug)
        anonymous = row["anonymous"]
        protected |= not anonymous
        bots.append(_select_bot(row, identity, anonymous, managed))
    oidc = _settings(product, "oidc")
    oidc_enabled = _enabled(oidc, protected)
    if protected and not oidc_enabled:
        raise SelectionError("Protected Pipecat bots require OIDC")
    if (
        managed
        and oidc_enabled
        and (
            oidc.get("clientId", "pipecat") != "pipecat"
            or oidc.get("secretName", "pipecat-oidc-secret") != "pipecat-oidc-secret"
            or oidc.get("secretKey", "clientSecret") != "clientSecret"
        )
    ):
        raise SelectionError("Pipecat OIDC requires the reviewed client and Secret reference")
    turn = _settings(product, "turn")
    turn_enabled = _enabled(turn, False)
    if (
        managed
        and turn_enabled
        and turn.get("secretName", "pipecat-turn-secret") != "pipecat-turn-secret"
    ):
        raise SelectionError("Managed Pipecat TURN requires the reviewed Secret reference")
    return PipecatSelection(oidc_enabled, turn_enabled, tuple(bots), managed)


def _select_bot(
    row: dict[str, object], identity: str, anonymous: bool, managed: bool
) -> PipecatBot:
    name = f"pipecat-bot-{identity}"
    if not anonymous and "gatewaySecretRef" in row:
        raise SelectionError("Protected Pipecat bots cannot select a gateway key")
    gateway = (
        anonymous
        and _managed_ref(row.get("gatewaySecretRef"), name, "gatewayApiKey", True)
        and managed
    )
    if gateway:
        permissions = row.get("gatewayPermissions", [])
        if not isinstance(permissions, list):
            raise SelectionError("Invalid Pipecat gateway permissions")
        gateway = bool(permissions)
        if gateway:
            _validate_grants(row)
        elif row.get("sessionsEnabled", False):
            raise SelectionError(
                "Enabled anonymous Pipecat sessions require explicit gateway grants"
            )
    fields = []
    for direction in ("stt", "tts"):
        speech = _settings(row, direction)
        field = f"{direction}ApiKey"
        if _managed_ref(speech.get("secretRef"), name, field, False) and managed:
            fields.append(field)
    return PipecatBot(identity, gateway, tuple(fields))


def _validate_grants(row: dict[str, object]) -> None:
    model = row.get("modelId")
    permissions = row.get("gatewayPermissions")
    if (
        not isinstance(model, str)
        or not _MODEL.fullmatch(model)
        or not isinstance(permissions, list)
        or not all(isinstance(item, str) for item in permissions)
    ):
        raise SelectionError("Managed Pipecat bots require explicit canonical model grants")
    required = {"llm:invoke", f"model:{model}:invoke"}
    if (
        len(permissions) != len(set(permissions))
        or not required.issubset(permissions)
        or any(item not in required and not _PROVIDER_GRANT.fullmatch(item) for item in permissions)
    ):
        raise SelectionError("Pipecat grants must include only invocation and explicit company-use")


def _managed_ref(value: object, name: str, key: str, default: bool) -> bool:
    if value is None:
        return default
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "key"}
        or any(not isinstance(item, str) or not item for item in value.values())
    ):
        raise SelectionError("Invalid Pipecat Secret reference")
    if value == {"name": name, "key": key}:
        return True
    if value["name"].startswith("pipecat-bot-"):
        raise SelectionError("Pipecat managed Secret reference does not match its bot and field")
    return False


def _settings(product: dict[str, object], name: str) -> dict[str, object]:
    value = product.get(name, {})
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SelectionError("Invalid Pipecat feature settings")
    return cast(dict[str, object], value)


def _enabled(settings: dict[str, object], default: bool) -> bool:
    enabled = settings.get("enabled", default)
    if type(enabled) is not bool:
        raise SelectionError("Pipecat feature enabled value must be a boolean")
    return enabled
