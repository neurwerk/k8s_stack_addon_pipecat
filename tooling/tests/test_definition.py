"""Real parser/engine/chart checks for bounded Pipecat records and independent features."""

import hashlib
import os
import subprocess
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from itertools import product
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from k8s_stack_tooling.addons import (
    ContractError,
    Selection,
    check,
    check_secret_deliveries,
    expand_setup,
    load_manifest,
)
from k8s_stack_tooling.addons.helm_images import validate_helm_images
from test_pipecat import PRODUCT, selection

from pipecat_tooling.cli import main
from pipecat_tooling.selection import SelectionError, validate_selection

ROOT = Path(__file__).resolve().parents[2]


def render(values: dict, namespace: str, mode: dict | None = None) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(ROOT / "charts/pipecat/shared"),
            "--namespace",
            namespace,
            "--values",
            "-",
        ],
        input=yaml.safe_dump({"pipecat": values, **(mode or {})}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [row for row in yaml.safe_load_all(result.stdout) if row]


def test_shared_parser_and_staged_publication_boundary(capsys):
    assert main([str(ROOT), "--list-packages"]) == 0
    assert "releases/pipecat/secret-sync" in capsys.readouterr().out
    check(ROOT)
    with pytest.raises(ContractError):
        check(ROOT, installable=True)


def test_empty_catalog_has_no_credentials_or_disabled_waits():
    addon = load_manifest(ROOT / "release/manifest.yaml")
    data = selection().model_dump(by_alias=True)
    data["addons"][0]["features"] = {}
    data["addons"][0]["instances"] = []
    selected = Selection.model_validate(data)
    validate_selection(addon, selected, {"enabled": True, "bots": []})
    setup = expand_setup(addon, selected.addons[0])
    assert not setup.records and not setup.input_groups and not setup.readers
    assert not setup.deliveries


def test_machine_request_does_not_supply_its_own_approval():
    addon = load_manifest(ROOT / "release/manifest.yaml")
    values = deepcopy(PRODUCT)
    values.pop("machineIdentityApprovals")
    with pytest.raises(SelectionError, match="client approval"):
        validate_selection(addon, selection(), values)


@pytest.mark.parametrize("gateway,stt,tts", list(product((False, True), repeat=3)))
def test_independent_bot_fields_match_actual_eso_without_voice(gateway, stt, tts):
    addon = load_manifest(ROOT / "release/manifest.yaml")
    selected = selection()
    selected.addons[0].features = {"oidc": True, "managed-oidc": True}
    selected.addons[0].instances[0].features = {
        "runtime": gateway or stt or tts,
        "gateway": gateway,
        "stt": stt,
        "tts": tts,
    }
    values = deepcopy(PRODUCT)
    values["turn"]["enabled"] = False
    bot = values["bots"][0]
    if not gateway:
        bot["gatewaySecretRef"] = {"name": "existing-key", "key": "apiKey"}
    for direction, enabled in (("stt", stt), ("tts", tts)):
        bot[direction] = (
            {"secretRef": {"name": "pipecat-bot-voice-bot", "key": f"{direction}ApiKey"}}
            if enabled
            else {}
        )
    validate_selection(addon, selected, values)
    setup = expand_setup(addon, selected.addons[0])
    expected = {(record.path, key) for record in setup.records for key in record.fields}
    actual = set()
    deliveries = set()
    for namespace, mode in (
        ("pipecat", {}),
        ("auth-keycloak", {"identityOnly": True}),
        ("auth-keycloak-api-key-bridge", {"managedKeysOnly": True}),
    ):
        for resource in render(values, namespace, mode):
            if resource["kind"] == "ExternalSecret":
                deliveries.add((namespace, resource["metadata"]["name"]))
                actual.update(
                    (field["remoteRef"]["key"], field["remoteRef"]["property"])
                    for field in resource["spec"]["data"]
                )
    assert actual == expected
    assert deliveries == {(d.namespace, d.name) for d in setup.deliveries}
    assert not setup.handover
    # A client cannot turn an external/authless provider into a supplied credential.
    selected.addons[0].instances[0].features["stt"] = not stt
    with pytest.raises(SelectionError):
        validate_selection(addon, selected, values)


def test_maximum_catalog_expands_without_suffixing_shared_stores():
    addon = load_manifest(ROOT / "release/manifest.yaml")
    data = selection().model_dump(by_alias=True)
    data["addons"][0]["instances"] = [
        {
            "id": f"bot-{i}",
            "recipe": "bot",
            "features": {
                "runtime": True,
                "gateway": True,
                "stt": True,
                "tts": True,
            },
        }
        for i in range(32)
    ]
    selected = Selection.model_validate(data)
    setup = expand_setup(addon, selected.addons[0])
    assert len(setup.records) == 98
    assert len(setup.deliveries) == 99
    assert len(setup.readers) == 3
    assert all("bot-" not in delivery.secret_store for delivery in setup.deliveries)
    data["addons"][0]["instances"].append({"id": "overflow", "recipe": "bot"})
    with pytest.raises(ValueError):
        Selection.model_validate(data)


def test_real_setup_preserves_raw_keys_and_validates_isolated_verifiers():
    try:
        installed = version("openbao-stack-setup")
    except PackageNotFoundError:
        pytest.fail(
            "Required integration prerequisite missing: install openbao-stack-setup==0.8.0 "
            "from the declared release source or an explicit reviewed candidate wheel",
            pytrace=False,
        )
    assert installed == "0.8.0", "Required integration needs openbao-stack-setup==0.8.0"
    assert version("k8s-stack-tooling") == "0.15.0"
    from openbao_stack_setup import client as client_module
    from openbao_stack_setup import declarative

    addon = load_manifest(ROOT / "release/manifest.yaml")
    setups = {"pipecat": expand_setup(addon, selection().addons[0])}
    snapshots = {}
    client = Mock(spec=client_module.OpenBaoClient)
    client.read_secret.side_effect = snapshots.get
    plan = declarative.plan_credentials(
        client,
        setups,
        supplied={"pipecat": {"voice-bot-speech": {"sttApiKey": "synthetic-input"}}},
    )
    for write in plan.writes:
        snapshots[write.path] = client_module.SecretRecord(values=write.values, version=1)
    assert not declarative.plan_credentials(client, setups).writes
    readers = {reader.role: set(reader.records) for reader in declarative.compose_readers(setups)}
    assert readers["auth-keycloak-api-key-bridge"] == {"addons/pipecat/verifiers/voice-bot"}
    assert not declarative.handover_definitions(setups, {})
    snapshots["addons/pipecat/verifiers/voice-bot"].values["gatewayApiKeySha256"] = "a" * 64
    with pytest.raises(client_module.OpenBaoError):
        declarative.plan_credentials(client, setups)
    client.write_secret.assert_not_called()


def test_verifier_delivery_is_staged_before_machine_provisioning():
    addon = load_manifest(ROOT / "release/manifest.yaml")
    packages = {package.name: package for package in addon.spec.packages}
    assert "secret-sync" in packages["setup"].depends_on
    release = yaml.safe_load(
        (ROOT / "releases/pipecat/secret-sync/verifier-secrets.yaml").read_text()
    )
    assert not release["spec"].get("dependsOn")
    values = deepcopy(PRODUCT)
    values["turn"]["enabled"] = False
    delivered = render(values, "auth-keycloak-api-key-bridge", {"managedKeysOnly": True})
    assert any(row["kind"] == "ExternalSecret" for row in delivered)
    assert not any(row["kind"] == "ConfigMap" for row in delivered)
    grants = render(
        values,
        "auth-keycloak-api-key-bridge",
        {
            "managedKeysOnly": True,
            "registrationOnly": True,
        },
    )
    assert any(row["kind"] == "ConfigMap" for row in grants)
    assert not any(row["kind"] == "ExternalSecret" for row in grants)


@pytest.mark.parametrize("media_enabled", [False, True])
def test_all_effective_image_consumers_and_machine_approvals(media_enabled):
    if not os.environ.get("ADDON_BASE"):
        pytest.fail(
            "Required integration prerequisite missing: set ADDON_BASE explicitly", pytrace=False
        )
    base = Path(os.environ["ADDON_BASE"])
    assert (base / "release/manifest.yaml").is_file(), "ADDON_BASE must be a reviewed Base checkout"
    addon = load_manifest(ROOT / "release/manifest.yaml")
    values = yaml.safe_load((ROOT / "validation/values.yaml").read_text())
    values["pipecat"]["image"] = next(a.image for a in addon.spec.artifacts if a.id == "runtime")
    # Synthetic offline-only script reference; this test never downloads an archive.
    values["pipecat"]["bots"][0]["script"] = {
        "enabled": media_enabled,
        "endpoint": "https://storage.example.com",
        "bucket": "example",
        "key": "fixture.tar.gz",
        "sha256": hashlib.sha256(b"synthetic-fixture").hexdigest(),
        "secretName": "existing-script-reader",
    }
    values["pipecat"]["turn"]["enabled"] = media_enabled
    values["pipecat"]["turn"].update(
        advertisedAddress="192.0.2.10",
        nodeSelector={"kubernetes.io/hostname": "example-turn-node"},
    )
    if not media_enabled:
        for bot in values["pipecat"]["bots"]:
            bot["sessionsEnabled"] = False
    roles = ["llm:invoke", "model:example-model:invoke"]
    values["authKeycloak"] = {
        "realm": "example",
        "adminUser": "admin",
        "agentgatewayClientRoles": roles,
    }
    values["pipecat"]["machineIdentityApprovals"] = {
        "public-example": {
            "clientId": "pipecat-bot-public-example",
            "realmRoles": [],
            "gatewayPermissions": list(roles),
            "audiences": ["agentgateway"],
        },
    }
    approval = {
        "clientApproval": {
            "clientId": "pipecat",
            "realmRoles": ["pipecat-user"],
            "audiences": ["agentgateway"],
            "gatewayPermissions": [],
            "webOrigins": ["https://voice.example.com"],
            "redirectUris": [
                "https://voice.example.com/simple_chatbot/auth/callback",
                "https://voice.example.com/public-example/auth/callback",
                "https://voice.example.com/not-ready/auth/callback",
            ],
        }
    }
    packages = {}
    for package in addon.spec.packages:
        result = subprocess.run(
            ["kustomize", "build", str(ROOT / package.path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        packages[package.name] = list(yaml.safe_load_all(result.stdout))
    configmaps = {}
    for item in addon.spec.inputs:
        content = values
        if item.name == "auth-keycloak-app-defaults":
            content = yaml.safe_load((base / "releases/keycloak/app-defaults.yaml").read_text())
        elif item.name == "pipecat-oidc-approval-values":
            content = approval
        configmaps[(item.namespace, item.name)] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"namespace": item.namespace, "name": item.name},
            "data": {"values.yaml": yaml.safe_dump(content)},
        }
    result = validate_helm_images(
        addon,
        packages,
        sources={
            ("GitRepository", "flux-system", "k8s-stack"): base,
            ("GitRepository", "flux-system", "pipecat-addon"): ROOT,
        },
        configmaps=configmaps,
        kube_version="1.34.1",
        api_versions=("external-secrets.io/v1",),
    )
    machines = result.rendered[("setup", "auth-keycloak", "keycloak-pipecat-machines")]
    job = next(item for item in machines if item["kind"] == "Job")
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["upsert-scoped-service-client"]
    assert next(e for e in container["env"] if e["name"] == "KC_CLIENT_SECRET")["valueFrom"] == {
        "secretKeyRef": {"name": "pipecat-bot-public-example-oidc", "key": "clientSecret"},
    }
    workloads = result.rendered[("app", "pipecat", "pipecat")]
    deployments = [item for item in workloads if item["kind"] == "Deployment"]
    assert (
        any(d["spec"]["template"]["spec"].get("initContainers") for d in deployments)
        == media_enabled
    )
    shared = result.rendered[("secret-sync", "pipecat", "pipecat-shared")]
    assert any(item["kind"] == "Deployment" for item in shared) == media_enabled


def test_actual_owned_runtime_store_and_base_reader_delivery_contract():
    if not os.environ.get("ADDON_BASE"):
        pytest.fail(
            "Required integration prerequisite missing: set ADDON_BASE explicitly", pytrace=False
        )
    base = Path(os.environ["ADDON_BASE"])
    addon = load_manifest(ROOT / "release/manifest.yaml")
    active = addon.model_copy(
        update={
            "spec": addon.spec.model_copy(
                update={"setup": expand_setup(addon, selection().addons[0])}
            ),
        }
    )
    objects = list(
        yaml.safe_load_all((ROOT / "releases/pipecat/secret-sync/runtime-store.yaml").read_text())
    )
    values = deepcopy(PRODUCT)
    values["image"] = next(a.image for a in addon.spec.artifacts if a.id == "runtime")
    values["turn"]["hostname"] = "turn.example.com"
    for namespace, mode in (
        ("pipecat", {}),
        ("auth-keycloak", {"identityOnly": True}),
        ("auth-keycloak-api-key-bridge", {"managedKeysOnly": True}),
    ):
        objects.extend(render(values, namespace, mode))
    base_resources = []
    for namespace in ("auth-keycloak", "auth-keycloak-api-key-bridge"):
        base_resources.extend(
            yaml.safe_load_all(
                (base / f"releases/openbao/secret-sync/{namespace}.yaml").read_text()
            )
        )
    check_secret_deliveries(active, objects, base_resources)
    drifted = deepcopy(objects)
    store = next(item for item in drifted if item["kind"] == "SecretStore")
    store["spec"]["provider"]["vault"]["auth"]["kubernetes"]["role"] = "unapproved-reader"
    with pytest.raises(ContractError, match="binding"):
        check_secret_deliveries(active, drifted, base_resources)
