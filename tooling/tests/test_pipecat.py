# Copyright (c) 2026 neurwerk
# SPDX-License-Identifier: MIT
# Domain cases moved from shared Tooling commit 4565779e7dacbf43c6f3f0868fc4c491d02b415b.
from copy import deepcopy
from dataclasses import asdict
from typing import Any

import pytest

from neurwerk_pipecat_tooling.selection import (
    SelectionError,
    select_pipecat,
    select_pipecat_values,
)

PRODUCT: dict[str, Any] = {
    "enabled": True,
    "secretSync": {"enabled": True},
    "oidc": {"enabled": True},
    "turn": {"enabled": True},
    "bots": [
        {
            "id": "voice-bot",
            "urlSlug": "voice_bot",
            "anonymous": True,
            "sessionsEnabled": False,
            "modelId": "example-model",
            "gatewayPermissions": [
                "llm:invoke",
                "model:example-model:invoke",
                "provider:example:company-use",
            ],
            "stt": {"secretRef": {"name": "pipecat-bot-voice-bot", "key": "sttApiKey"}},
        },
        {"id": "protected-bot", "anonymous": False, "sessionsEnabled": False},
    ],
}


@pytest.mark.parametrize("identity", ["../escape", "a/b", "a*", "voice_bot", "A", "a-", "a" * 41])
def test_bot_ids_cannot_escape_exact_paths(identity: str) -> None:
    product = deepcopy(PRODUCT)
    product["bots"][0]["id"] = identity
    with pytest.raises(SelectionError, match="Pipecat"):
        select_pipecat(product)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "protected-bot"},
        {"urlSlug": "protected-bot"},
        {"urlSlug": "assets"},
        {"anonymous": "true"},
        {"sessionsEnabled": "true"},
        {"gatewayPermissions": ["llm:invoke", "model:other:invoke"]},
        {"gatewayPermissions": ["llm:invoke", "model:example-model:invoke", "admin"]},
        {"gatewayPermissions": ["llm:invoke", "llm:invoke", "model:example-model:invoke"]},
        {"gatewayPermissions": ["llm:invoke", "model:example-model:invoke", "model:*:invoke"]},
        {"gatewaySecretRef": {"name": "pipecat-bot-other", "key": "gatewayApiKey"}},
        {"stt": {"secretRef": {"name": "pipecat-bot-voice-bot", "key": "gatewayApiKey"}}},
    ],
)
def test_invalid_or_broader_bot_selections_fail_closed(change: dict[str, Any]) -> None:
    product = deepcopy(PRODUCT)
    product["bots"][0].update(change)
    with pytest.raises(SelectionError, match="Pipecat"):
        select_pipecat(product)


def test_selection_matches_chart_refs_and_does_not_import_external_secrets() -> None:
    selected = select_pipecat(PRODUCT)
    assert asdict(selected.bots[0]) == {
        "id": "voice-bot",
        "gateway": True,
        "speech": ("sttApiKey",),
    }
    assert not selected.bots[1].gateway
    product = deepcopy(PRODUCT)
    product["bots"][0]["gatewaySecretRef"] = {"name": "existing-scoped-key", "key": "key"}
    product["bots"][0]["stt"]["secretRef"] = {"name": "existing-speech", "key": "token"}
    external = select_pipecat(product)
    assert not external.bots[0].gateway and not external.bots[0].speech
    assert external.paths("auth-keycloak-api-key-bridge") == ()
    assert external.paths("pipecat") == ("addons/pipecat/oidc", "addons/pipecat/turn")
    assert not select_pipecat({"enabled": True, "bots": []}).refresh_targets().external_secrets
    page_only = select_pipecat(
        {
            "enabled": True,
            "secretSync": {"enabled": True},
            "bots": [{"id": "page-only", "anonymous": True, "sessionsEnabled": False}],
        }
    )
    assert not page_only.refresh_targets().external_secrets


def test_namespace_grants_are_exact_and_no_raw_key_reaches_bridge_or_keycloak() -> None:
    selected = select_pipecat(PRODUCT)
    assert selected.paths("pipecat") == (
        "addons/pipecat/oidc",
        "addons/pipecat/turn",
        "addons/pipecat/bots/voice-bot",
    )
    assert selected.paths("auth-keycloak") == (
        "addons/pipecat/oidc",
        "addons/pipecat/machines/voice-bot",
    )
    assert selected.paths("auth-keycloak-api-key-bridge") == ("addons/pipecat/verifiers/voice-bot",)


def test_external_secrets_preserve_active_oidc_turn_refresh_without_credential_mutation() -> None:
    product = deepcopy(PRODUCT)
    product["secretSync"]["enabled"] = False
    product["oidc"]["secretName"] = "existing-login"
    product["turn"]["secretName"] = "existing-turn"
    selected = select_pipecat(product)
    targets = selected.refresh_targets()
    assert not targets.external_secrets
    assert all(
        not selected.paths(ns)
        for ns in ("pipecat", "auth-keycloak", "auth-keycloak-api-key-bridge")
    )
    assert targets.helm_releases == (
        ("auth-keycloak", "pipecat-oidc-settings"),
        ("auth-keycloak", "keycloak-pipecat-access"),
        ("auth-keycloak", "keycloak-pipecat-oidc"),
        ("pipecat", "pipecat-shared"),
    )
    assert targets.deployments == (
        ("pipecat", "pipecat-voice-bot"),
        ("pipecat", "pipecat-protected-bot"),
        ("pipecat", "pipecat-turn"),
    )


def test_anonymous_bots_can_offer_optional_oidc_login() -> None:
    product: dict[str, Any] = {
        "enabled": True,
        "secretSync": {"enabled": True},
        "oidc": {"enabled": True},
        "bots": [{"id": "public-bot", "anonymous": True}],
    }
    selected = select_pipecat(product)
    assert selected.paths("auth-keycloak") == ("addons/pipecat/oidc",)
    assert not selected.paths("auth-keycloak-api-key-bridge")
    assert ("auth-keycloak", "keycloak-pipecat-oidc") in selected.refresh_targets().helm_releases
    product["bots"] = [{"id": "protected-bot", "anonymous": False}]
    product["oidc"] = {"enabled": False}
    with pytest.raises(SelectionError, match="require OIDC"):
        select_pipecat(product)


@pytest.mark.parametrize("turn_enabled", [False, True])
@pytest.mark.parametrize("identity", ["turn", "product-values"])
def test_shared_resource_ids_are_reserved_before_resolution(
    turn_enabled: bool, identity: str
) -> None:
    with pytest.raises(SelectionError, match="Pipecat"):
        select_pipecat(
            {
                "enabled": True,
                "secretSync": {"enabled": True},
                "turn": {"enabled": turn_enabled},
                "bots": [{"id": identity, "anonymous": True}],
            }
        )


def test_fixed_value_sources_merge_in_helm_order() -> None:
    assert select_pipecat_values(
        {"pipecat": {"enabled": True, "oidc": {"clientId": "pipecat"}}},
        {"pipecat": PRODUCT},
    ) == select_pipecat(PRODUCT)
    split = deepcopy(PRODUCT)
    split["secretSync"] = {"stores": {"pipecat": "pipecat-openbao-secret-store"}}
    assert select_pipecat_values(
        {"pipecat": {"secretSync": {"enabled": True}}},
        {"pipecat": split},
    ) == select_pipecat(PRODUCT)
