"""Reviewed channel publication, concurrency, rollback and rollout boundaries."""

import copy
import hashlib
import io
import json
from unittest.mock import Mock

import pytest
import yaml
from botocore.exceptions import ClientError, ReadTimeoutError
from test_apps import change_args
from test_apps import client as client
from test_apps import source as source

from pipecat_tooling import apps, channel_controller, channels


class Store:
    bundle: apps.Bundle

    def __init__(self):
        self.objects = {}
        self.writes = []
        self.before_put = lambda kwargs: None
        self.after_put = lambda kwargs: None

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[key]
        return {
            "Body": io.BytesIO(data),
            "ETag": hashlib.sha256(data).hexdigest(),
            "ContentLength": len(data),
        }

    def put_object(self, **kwargs):
        self.before_put(kwargs)
        key = kwargs["Key"]
        current = self.objects.get(key)
        if (kwargs.get("IfNoneMatch") == "*" and current is not None) or (
            "IfMatch" in kwargs
            and (current is None or hashlib.sha256(current).hexdigest() != kwargs["IfMatch"])
        ):
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        self.objects[key] = kwargs["Body"]
        self.writes.append(key)
        self.after_put(kwargs)

    def close(self):
        pass


@pytest.fixture
def store(source):
    store = Store()
    bundle = apps.package(source, "assistant")
    store.objects[f"pipecat/assistant/{bundle.sha256}.tar.gz"] = bundle.data
    store.bundle = bundle
    return store


def promote(store):
    return channels.promote(store, "apps", "assistant", "dev", store.bundle.sha256, "0.2.0")


def configure(client):
    value = yaml.safe_load(client[1].read_text())
    value["pipecat"]["bots"][0]["script"] = {
        "enabled": True,
        "channel": "dev",
        "runtimeVersion": "0.2.0",
        "bucket": "apps",
        "endpoint": "https://s3.example.com",
        "secretName": "reader",
    }
    client[1].write_text(yaml.safe_dump(value))
    return client[1].read_bytes()


def test_channel_publish_leaves_client_bytes_unchanged(source, client, store, monkeypatch):
    before = configure(client)
    monkeypatch.setattr(apps.boto3, "client", lambda *a, **kw: store)
    args = change_args("publish", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
    ]
    assert apps.main(args) == 0
    assert client[1].read_bytes() == before
    record, _ = channels.read_pointer(store, "apps", "assistant", "dev")
    assert record is not None
    assert record["sha256"] == store.bundle.sha256
    assert (
        json.loads(
            store.objects[f"pipecat/assistant/channels/dev/history/{record['revision']}.json"]
        )
        == record
    )


@pytest.mark.parametrize("failure", ["upload", "corrupt", "history"])
def test_failed_publication_does_not_advance_channel_or_edit_client(
    source, client, store, monkeypatch, failure
):
    previous = promote(store)
    before = configure(client)
    (source / "assistant/bot.py").write_text("print('new app')\n")

    def fail(kwargs):
        if (failure == "upload" and kwargs["Key"].endswith(".tar.gz")) or (
            failure == "history" and "/history/" in kwargs["Key"]
        ):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    store.before_put = fail
    if failure == "corrupt":
        store.after_put = lambda kw: (
            store.objects.update({kw["Key"]: b"corrupt"}) if kw["Key"].endswith(".tar.gz") else None
        )
    monkeypatch.setattr(apps.boto3, "client", lambda *a, **kw: store)
    args = change_args("publish", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
    ]
    assert apps.main(args) == 1
    assert client[1].read_bytes() == before
    assert channels.read_pointer(store, "apps", "assistant", "dev")[0] == previous


@pytest.mark.parametrize("initialized", [False, True])
def test_two_publishers_cannot_overwrite_each_other(store, initialized):
    if initialized:
        promote(store)
    winner = None

    def race(kwargs):
        nonlocal winner
        if kwargs["Key"].endswith("current.json"):
            store.before_put = lambda _: None
            winner = promote(store)

    store.before_put = race
    with pytest.raises(apps.AppError, match="concurrently"):
        promote(store)
    assert channels.read_pointer(store, "apps", "assistant", "dev")[0] == winner


@pytest.mark.parametrize("retried", [False, True])
def test_accepted_write_timeout_is_resolved_by_readback(store, retried):
    def timeout(kwargs):
        if kwargs["Key"].endswith("current.json"):
            if retried:
                raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
            raise ReadTimeoutError(endpoint_url="https://s3.example.com")

    store.after_put = timeout
    result = promote(store)
    assert channels.read_pointer(store, "apps", "assistant", "dev")[0] == result


def test_rollback_reverifies_existing_archive_and_keeps_history_without_local_source(
    client, store, source, monkeypatch
):
    first = promote(store)
    (source / "assistant/bot.py").write_text("print('new release')\n")
    store.bundle = apps.package(source, "assistant")
    store.objects[f"pipecat/assistant/{store.bundle.sha256}.tar.gz"] = store.bundle.data
    newer = promote(store)
    assert newer["sha256"] != first["sha256"]
    before = configure(client)
    monkeypatch.setattr(channels, "s3_client", lambda *a, **kw: store)
    args = change_args("promote", client) + [
        "--sha256",
        first["sha256"],
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
    ]
    assert apps.main(args) == 0
    second = channels.read_pointer(store, "apps", "assistant", "dev")[0]
    assert second is not None
    assert second["revision"] != first["revision"]
    assert second["previous"] == newer["revision"]
    assert second["sha256"] == first["sha256"]
    assert client[1].read_bytes() == before
    store.objects[first["key"]] = b"bad replacement"
    assert apps.main(args) == 1
    assert channels.read_pointer(store, "apps", "assistant", "dev")[0] == second


@pytest.mark.parametrize(
    "change", ["app", "runtimeVersion", "oversized", "invalid-json", "checksum"]
)
def test_invalid_channels_never_replace_last_resolution(store, change):
    record = promote(store)
    kube = Mock()
    old = deployment({**record, "sha256": "0" * 64})
    kube.get.return_value = copy.deepcopy(old)
    key = channels.channel_key("assistant", "dev")
    if change == "oversized":
        store.objects[key] = b" " * (channels.MAX_POINTER + 1)
    elif change == "invalid-json":
        store.objects[key] = b"["
    elif change == "checksum":
        store.objects[record["key"]] = b"corrupt"
    else:
        record[change] = "different" if change == "app" else "9.0.0"
        store.objects[key] = json.dumps(record).encode()
    result = channel_controller.reconcile([config()], kube, lambda _: store)
    assert result["assistant"] != "ready"
    kube.update.assert_not_called()
    assert kube.get.return_value == old


def config():
    return {
        "app": "assistant",
        "channel": "dev",
        "runtimeVersion": "0.2.0",
        "endpoint": "https://s3.example.com",
        "bucket": "apps",
    }


def selected(record):
    return {
        **channel_controller.source_policy(config()),
        "key": record["key"],
        "sha256": record["sha256"],
    }


def deployment(record):
    return {
        "metadata": {"generation": 2, "resourceVersion": "1"},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {
                    "annotations": {
                        "checksum/config": "keep",
                        channel_controller.ANNOTATION: channel_controller.encode(selected(record)),
                    }
                },
                "spec": {
                    "initContainers": [
                        {
                            "name": "load-script",
                            "env": [
                                {"name": "S3_KEY", "value": record["key"]},
                                {"name": "BUNDLE_SHA256", "value": record["sha256"]},
                                {"name": "S3_ENDPOINT", "value": config()["endpoint"]},
                                {"name": "S3_BUCKET", "value": "apps"},
                                {"name": "BUNDLE_APP_ID", "value": "assistant"},
                                {"name": "BUNDLE_CHANNEL", "value": "dev"},
                                {"name": "BUNDLE_RUNTIME_VERSION", "value": "0.2.0"},
                                {
                                    "name": "BUNDLE_CHANNEL_RELEASE",
                                    "valueFrom": {
                                        "fieldRef": {
                                            "fieldPath": (
                                                f"metadata.annotations['{channel_controller.ANNOTATION}']"
                                            ),
                                        }
                                    },
                                },
                            ],
                        }
                    ]
                },
            },
        },
        "status": {
            "observedGeneration": 2,
            "replicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }


def test_controller_conflict_restarts_with_fresh_intent(store, source, monkeypatch):
    record = promote(store)
    state = deployment(record)
    annotations = state["spec"]["template"]["metadata"]["annotations"]
    del annotations[channel_controller.ANNOTATION]
    patches = []
    latest = record

    def run(command):
        nonlocal latest
        assert "pipecat-assistant" in command
        if "get" in command:
            return json.dumps(state)
        body = json.loads(command[-1])
        patches.append(body)
        if len(patches) == 1:
            state["metadata"]["resourceVersion"] = "2"
            annotations["other-controller"] = "preserve"
            (source / "assistant/bot.py").write_text("print('newer intent')\n")
            store.bundle = apps.package(source, "assistant")
            store.objects[f"pipecat/assistant/{store.bundle.sha256}.tar.gz"] = store.bundle.data
            latest = promote(store)
        if body["metadata"]["resourceVersion"] != state["metadata"]["resourceVersion"]:
            raise apps.AppError("conflict")
        assert set(body) == {"metadata", "spec"}
        assert body["spec"] == {
            "template": {
                "metadata": {
                    "annotations": {
                        channel_controller.ANNOTATION: channel_controller.encode(selected(latest))
                    }
                }
            }
        }
        annotations.update(body["spec"]["template"]["metadata"]["annotations"])
        state["metadata"].update(resourceVersion="3", generation=3)
        return "patched"

    monkeypatch.setattr(channel_controller, "run", run)
    kube = channel_controller.Kubernetes("pipecat")
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {
        "assistant": "verification-or-rollout-check-failed"
    }
    assert channel_controller.ANNOTATION not in annotations
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {
        "assistant": "awaiting-rollout"
    }
    assert annotations["checksum/config"] == "keep"
    assert annotations["other-controller"] == "preserve"
    assert latest["sha256"] != record["sha256"]
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {
        "assistant": "awaiting-rollout"
    }
    state["status"]["observedGeneration"] = 3
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {"assistant": "ready"}
    assert len(patches) == 2


@pytest.mark.parametrize(
    "change", ["old-generation", "old-replicas", "unavailable", "wrong-bundle"]
)
def test_old_or_failed_rollout_never_reports_ready(store, change):
    record = promote(store)
    obj = deployment(record)
    if change == "old-generation":
        obj["status"]["observedGeneration"] = 1
    elif change == "old-replicas":
        obj["status"]["updatedReplicas"] = 0
    elif change == "unavailable":
        obj["status"]["unavailableReplicas"] = 1
    else:
        obj["spec"]["template"]["metadata"]["annotations"][channel_controller.ANNOTATION] = "wrong"
    assert not channel_controller.rollout_ready(obj, selected(record))


@pytest.mark.parametrize(
    "field",
    [
        "S3_ENDPOINT",
        "S3_BUCKET",
        "BUNDLE_APP_ID",
        "BUNDLE_CHANNEL",
        "BUNDLE_RUNTIME_VERSION",
        "BUNDLE_CHANNEL_RELEASE",
    ],
)
def test_stale_controller_cannot_patch_or_report_ready_for_changed_policy(store, field):
    record = promote(store)
    obj = deployment(record)
    env = obj["spec"]["template"]["spec"]["initContainers"][0]["env"]
    env[:] = [entry for entry in env if entry["name"] != field]
    kube = Mock()
    kube.get.return_value = obj
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {
        "assistant": "deployment-policy-changed"
    }
    assert not channel_controller.rollout_ready(obj, selected(record))
    kube.update.assert_not_called()


def test_channel_move_during_verification_waits_for_next_tick(store, monkeypatch):
    record = promote(store)
    kube = Mock()
    kube.get.return_value = deployment({**record, "sha256": "0" * 64})
    verify = channel_controller.verify_bundle

    def moving(*args):
        key = verify(*args)
        promote(store)
        return key

    monkeypatch.setattr(channel_controller, "verify_bundle", moving)
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {
        "assistant": "channel-changed-during-verification"
    }
    kube.update.assert_not_called()


def test_policy_change_during_verification_conflicts_then_stops(store, monkeypatch):
    record = promote(store)
    state = deployment({**record, "sha256": "0" * 64})
    kube = Mock()
    kube.get.side_effect = lambda _: copy.deepcopy(state)
    verify = channel_controller.verify_bundle

    def changed(*args):
        key = verify(*args)
        state["metadata"]["resourceVersion"] = "2"
        env = state["spec"]["template"]["spec"]["initContainers"][0]["env"]
        next(entry for entry in env if entry["name"] == "BUNDLE_CHANNEL")["value"] = "stable"
        return key

    def update(name, version, selection):
        assert version != state["metadata"]["resourceVersion"]
        raise apps.AppError("conflict")

    monkeypatch.setattr(channel_controller, "verify_bundle", changed)
    kube.update.side_effect = update
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {
        "assistant": "verification-or-rollout-check-failed"
    }
    assert channel_controller.reconcile([config()], kube, lambda _: store) == {
        "assistant": "deployment-policy-changed"
    }
    assert kube.update.call_count == 1


def test_one_invalid_channel_does_not_block_other_followers(store):
    record = promote(store)
    kube = Mock()
    kube.get.return_value = deployment(record)
    store.objects[channels.channel_key("broken", "dev")] = b"invalid"
    assert channel_controller.reconcile(
        [{**config(), "app": "broken"}, config()], kube, lambda _: store
    ) == {"broken": "verification-or-rollout-check-failed", "assistant": "ready"}
    kube.update.assert_not_called()


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE", "MAX_EXPANDED", "MAX_FILES"])
def test_existing_archives_are_bounded_before_promotion(store, monkeypatch, limit):
    monkeypatch.setattr(channels, limit, 1)
    with pytest.raises(apps.AppError):
        promote(store)
    assert channels.channel_key("assistant", "dev") not in store.objects


@pytest.mark.parametrize("change", ["app", "runtimeVersion"])
def test_valid_checksum_cannot_hide_wrong_manifest(source, store, change):
    bundle = store.bundle
    # Rebuild a checksum-valid archive with the wrong app/runtime identity.
    import tarfile

    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz") as old,
        tarfile.open(fileobj=out, mode="w:gz") as new,
    ):
        for item in old:
            stream = old.extractfile(item)
            assert stream is not None
            content = stream.read()
            if item.name == "manifest.json":
                manifest = json.loads(content)
                manifest["id" if change == "app" else change] = (
                    "other" if change == "app" else "9.0.0"
                )
                content = json.dumps(manifest).encode()
            item.size = len(content)
            new.addfile(item, io.BytesIO(content))
    digest = hashlib.sha256(out.getvalue()).hexdigest()
    store.objects[f"pipecat/assistant/{digest}.tar.gz"] = out.getvalue()
    with pytest.raises(apps.AppError):
        channels.promote(store, "apps", "assistant", "dev", digest, "0.2.0")
    assert channels.channel_key("assistant", "dev") not in store.objects
