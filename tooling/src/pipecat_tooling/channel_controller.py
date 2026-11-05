"""Resolve configured Pipecat channels into controller-owned rollout annotations."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from pipecat_tooling.apps import AppError, endpoint_url, identifier, run
from pipecat_tooling.channels import read_pointer, s3_client, verify_bundle

ANNOTATION = "pipecat.neurwerk.com/channel-release"


def source_policy(config: dict) -> dict:
    return {
        name: config[name] for name in ("app", "endpoint", "bucket", "channel", "runtimeVersion")
    }


def encode(selected: dict) -> str:
    return json.dumps(selected, sort_keys=True, separators=(",", ":"))


def policy_matches(deployment: dict, selected: dict) -> bool:
    loader = next(
        (
            container
            for container in deployment["spec"]["template"]["spec"].get("initContainers", [])
            if container["name"] == "load-script"
        ),
        {},
    )
    env = {entry["name"]: entry for entry in loader.get("env", [])}
    names = {
        "app": "BUNDLE_APP_ID",
        "endpoint": "S3_ENDPOINT",
        "bucket": "S3_BUCKET",
        "channel": "BUNDLE_CHANNEL",
        "runtimeVersion": "BUNDLE_RUNTIME_VERSION",
    }
    return all(env.get(name, {}).get("value") == selected[key] for key, name in names.items()) and (
        env.get("BUNDLE_CHANNEL_RELEASE", {})
        .get("valueFrom", {})
        .get("fieldRef", {})
        .get("fieldPath")
        == f"metadata.annotations['{ANNOTATION}']"
    )


def rollout_ready(deployment: dict, selected: dict) -> bool:
    spec, status = deployment["spec"], deployment.get("status", {})
    desired = spec.get("replicas", 1)
    annotations = spec["template"].get("metadata", {}).get("annotations", {})
    return bool(
        desired > 0
        and policy_matches(deployment, selected)
        and annotations.get(ANNOTATION) == encode(selected)
        and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
        and status.get("updatedReplicas", 0) == desired
        and status.get("replicas", 0) == desired
        and status.get("availableReplicas", 0) == desired
        and not status.get("unavailableReplicas", 0)
    )


class Kubernetes:
    def __init__(self, namespace: str):
        self.command = ["kubectl", "--namespace", namespace, "--request-timeout=20s"]

    def get(self, name: str) -> dict:
        return json.loads(run(self.command + ["get", "deployment", name, "-o", "json"]))

    def update(self, name: str, resource_version: str, selected: dict) -> None:
        body = {
            "metadata": {"resourceVersion": resource_version},
            "spec": {"template": {"metadata": {"annotations": {ANNOTATION: encode(selected)}}}},
        }
        run(self.command + ["patch", "deployment", name, "--type=merge", "-p", json.dumps(body)])


def connect(config: dict):
    endpoint_url(config["endpoint"], runtime=True)
    return s3_client(
        config["endpoint"],
        aws_access_key_id=Path(config["accessKeyFile"]).read_text().strip(),
        aws_secret_access_key=Path(config["secretKeyFile"]).read_text().strip(),
    )


def reconcile(configs: list[dict], kube: Kubernetes, client_factory=connect) -> dict[str, str]:
    results = {}
    for config in configs:
        app = identifier(config["app"])
        client = None
        try:
            client = client_factory(config)
            record, etag = read_pointer(client, config["bucket"], app, config["channel"])
            if record is None:
                results[app] = "channel-missing"
                continue
            if record["runtimeVersion"] != config["runtimeVersion"]:
                raise AppError("Channel requires a different runtime contract")
            selected = {**source_policy(config), "key": record["key"], "sha256": record["sha256"]}
            name = f"pipecat-{app}"
            deployment = kube.get(name)
            if not policy_matches(deployment, selected):
                results[app] = "deployment-policy-changed"
                continue
            annotations = deployment["spec"]["template"].get("metadata", {}).get("annotations", {})
            if annotations.get(ANNOTATION) != encode(selected):
                verify_bundle(
                    client, config["bucket"], app, record["sha256"], config["runtimeVersion"]
                )
                latest, latest_etag = read_pointer(client, config["bucket"], app, config["channel"])
                if latest_etag != etag or latest != record:
                    results[app] = "channel-changed-during-verification"
                    continue
                # A conflict is retried with fresh deployment state AND channel intent next tick.
                kube.update(name, deployment["metadata"]["resourceVersion"], selected)
                results[app] = "awaiting-rollout"
            else:
                results[app] = (
                    "ready" if rollout_ready(deployment, selected) else "awaiting-rollout"
                )
        except (
            BotoCoreError,
            ClientError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.TimeoutExpired,
        ):
            results[app] = "verification-or-rollout-check-failed"
        finally:
            if client is not None:
                client.close()
    return results


def main() -> None:
    config = json.loads(Path(os.environ["CHANNEL_CONFIG"]).read_text())
    interval = config["intervalSeconds"]
    if type(interval) is not int or not 10 <= interval <= 300:
        raise SystemExit("Invalid channel polling interval")
    kube = Kubernetes(os.environ["POD_NAMESPACE"])
    previous = None
    while True:
        state = reconcile(config["bots"], kube)
        if state != previous:
            print(json.dumps(state, sort_keys=True), flush=True)
            previous = state
        time.sleep(interval)
