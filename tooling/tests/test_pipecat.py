# Copyright (c) 2026 neurwerk
# SPDX-License-Identifier: MIT
# Domain cases moved from shared Tooling commit 4565779e7dacbf43c6f3f0868fc4c491d02b415b.
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from k8s_stack_tooling.addons import Selection, expand_setup, load_manifest

from pipecat_tooling.selection import (
    SelectionError,
    select_pipecat,
    select_pipecat_values,
    validate_selection,
)

ROOT = Path(__file__).resolve().parents[2]


def selection() -> Selection:
    return Selection.model_validate(
        {
            "apiVersion": "addons.neurwerk.com/v1alpha2",
            "kind": "AddonSelection",
            "baseSourceRef": {
                "kind": "GitRepository",
                "namespace": "flux-system",
                "name": "k8s-stack",
            },
            "addons": [
                {
                    "id": "pipecat",
                    "sourceRef": {
                        "kind": "GitRepository",
                        "namespace": "flux-system",
                        "name": "pipecat-addon",
                    },
                    "features": {
                        "oidc": True,
                        "turn": True,
                        "managed-oidc": True,
                        "managed-turn": True,
                    },
                    "instances": [
                        {
                            "id": "voice-bot",
                            "recipe": "bot",
                            "features": {"runtime": True, "gateway": True, "stt": True},
                        },
                        {"id": "protected-bot", "recipe": "bot"},
                    ],
                }
            ],
        }
    )


def setup(selected: Selection):
    addon = load_manifest(ROOT / "release/manifest.yaml")
    return expand_setup(addon, selected.addons[0])


PRODUCT: dict[str, Any] = {
    "enabled": True,
    "secretSync": {"enabled": True},
    "oidc": {"enabled": True},
    "turn": {
        "enabled": True,
        "peerAddressRanges": ["10.42.0.1-10.42.0.254"],
        "nodeSelector": {"kubernetes.io/hostname": "example-turn-node"},
    },
    "machineIdentityApprovals": {
        "voice-bot": {
            "clientId": "pipecat-bot-voice-bot",
            "realmRoles": [],
            "gatewayPermissions": [
                "llm:invoke",
                "model:example-model:invoke",
                "provider:example:company-use",
            ],
            "audiences": ["agentgateway"],
        },
    },
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
        {"enabled": "false"},
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
    assert not select_pipecat({"enabled": True, "bots": []}).bots
    page_only = select_pipecat(
        {
            "enabled": True,
            "secretSync": {"enabled": True},
            "bots": [{"id": "page-only", "anonymous": True, "sessionsEnabled": False}],
        }
    )
    assert not page_only.bots[0].gateway and not page_only.bots[0].speech


def test_namespace_grants_are_exact_and_no_raw_key_reaches_bridge_or_keycloak() -> None:
    selected = selection()
    addon = load_manifest(ROOT / "release/manifest.yaml")
    validate_selection(addon, selected, PRODUCT)
    expanded = setup(selected)
    paths = {record.id: record.path for record in expanded.records}
    readers = {
        reader.namespace: {paths[name] for name in reader.records} for reader in expanded.readers
    }
    assert readers["pipecat"] == {
        "addons/pipecat/oidc",
        "addons/pipecat/turn",
        "addons/pipecat/bots/voice-bot",
    }
    assert readers["auth-keycloak"] == {
        "addons/pipecat/oidc",
        "addons/pipecat/machines/voice-bot",
    }
    assert readers["auth-keycloak-api-key-bridge"] == {"addons/pipecat/verifiers/voice-bot"}
    assert {delivery.secret_store for delivery in expanded.deliveries} == {
        "pipecat-openbao-secret-store",
        "auth-keycloak-openbao-secret-store",
        "auth-keycloak-api-key-bridge-openbao-secret-store",
    }


def test_external_secrets_do_not_require_managed_credentials() -> None:
    product = deepcopy(PRODUCT)
    product["secretSync"]["enabled"] = False
    product["oidc"]["secretName"] = "existing-login"
    product["turn"]["secretName"] = "existing-turn"
    selected = selection()
    selected.addons[0].features.update({"managed-oidc": False, "managed-turn": False})
    selected.addons[0].instances[0].features = {}
    validate_selection(load_manifest(ROOT / "release/manifest.yaml"), selected, product)
    expanded = setup(selected)
    assert not expanded.records and not expanded.deliveries and not expanded.readers


@pytest.mark.parametrize("anonymous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_all_bots_require_oidc_even_when_disabled(anonymous: bool, enabled: bool) -> None:
    product: dict[str, Any] = {
        "enabled": True,
        "secretSync": {"enabled": True},
        "oidc": {"enabled": True},
        "bots": [{"id": "public-bot", "anonymous": anonymous, "enabled": enabled}],
    }
    selected = select_pipecat(product)
    assert selected.oidc and not selected.bots[0].gateway
    product["oidc"] = {"enabled": False}
    with pytest.raises(SelectionError, match="require OIDC"):
        select_pipecat(product)


def test_disabled_bots_keep_identity_and_credential_setup_ownership() -> None:
    product = deepcopy(PRODUCT)
    for bot in product["bots"]:
        bot["enabled"] = False
    assert select_pipecat(product) == select_pipecat(PRODUCT)
    validate_selection(load_manifest(ROOT / "release/manifest.yaml"), selection(), product)


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
