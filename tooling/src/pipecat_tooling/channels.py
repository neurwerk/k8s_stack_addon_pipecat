"""Pipecat release channels: verified bundles, immutable history and conditional promotion."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TypedDict, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from pipecat_tooling.apps import (
    MAX_ARCHIVE,
    MAX_EXPANDED,
    MAX_FILES,
    AppError,
    identifier,
    validate_requirements,
)

MAX_POINTER = 8192
SHA = re.compile(r"[a-f0-9]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]*)")


class ChannelRecord(TypedDict):
    version: int
    app: str
    channel: str
    revision: str
    previous: str | None
    publishedAt: str
    key: str
    sha256: str
    runtimeVersion: str


def s3_client(endpoint: str, **credentials):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
        **credentials,
    )


def channel_key(app: str, channel: str) -> str:
    return f"pipecat/{identifier(app)}/channels/{identifier(channel)}/current.json"


def read_object(client, bucket: str, key: str, limit: int) -> tuple[bytes, str]:
    response = client.get_object(Bucket=bucket, Key=key)
    try:
        if response.get("ContentLength", 0) > limit:
            raise AppError("Stored object exceeds its size limit")
        data = response["Body"].read(limit + 1)
        if len(data) > limit:
            raise AppError("Stored object exceeds its size limit")
        return data, response["ETag"]
    finally:
        response["Body"].close()


def decode_pointer(data: bytes, app: str, channel: str) -> ChannelRecord:
    try:
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "app",
                "channel",
                "revision",
                "previous",
                "publishedAt",
                "key",
                "sha256",
                "runtimeVersion",
            }
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["app"] != app
            or value["channel"] != channel
            or not isinstance(value["sha256"], str)
            or not SHA.fullmatch(value["sha256"])
            or value["key"] != f"pipecat/{app}/{value['sha256']}.tar.gz"
            or not isinstance(value["runtimeVersion"], str)
            or not VERSION.fullmatch(value["runtimeVersion"])
            or not isinstance(value["revision"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", value["revision"])
            or (
                value["previous"] is not None
                and (
                    not isinstance(value["previous"], str)
                    or not re.fullmatch(r"[a-f0-9]{32}", value["previous"])
                )
            )
            or not isinstance(value["publishedAt"], str)
            or len(value["publishedAt"]) > 40
        ):
            raise ValueError
        datetime.fromisoformat(value["publishedAt"])
        return cast(ChannelRecord, value)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise AppError("Invalid release channel record") from None


def read_pointer(
    client, bucket: str, app: str, channel: str
) -> tuple[ChannelRecord | None, str | None]:
    try:
        data, etag = read_object(client, bucket, channel_key(app, channel), MAX_POINTER)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NoSuchKey":
            return None, None
        raise
    return decode_pointer(data, app, channel), etag


def verify_bundle(client, bucket: str, app: str, digest: str, runtime: str) -> str:
    if not SHA.fullmatch(digest) or not VERSION.fullmatch(runtime):
        raise AppError("An exact bundle checksum and runtime contract are required")
    key = f"pipecat/{identifier(app)}/{digest}.tar.gz"
    data, _ = read_object(client, bucket, key, MAX_ARCHIVE)
    if hashlib.sha256(data).hexdigest() != digest:
        raise AppError("Stored bundle checksum mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            expanded = compressed.read(MAX_EXPANDED + MAX_FILES * 2048 + 1)
        if len(expanded) > MAX_EXPANDED + MAX_FILES * 2048:
            raise AppError("Expanded bundle limit exceeded")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            names: set[str] = set()
            size = 0
            for member in archive:
                path = PurePosixPath(member.name)
                size += member.size
                if (
                    not member.isfile()
                    or member.sparse is not None
                    or path.is_absolute()
                    or not path.parts
                    or str(path) != member.name
                    or "\\" in member.name
                    or any(part.startswith(".") for part in path.parts)
                    or member.name in names
                    or len(names) >= MAX_FILES
                    or size > MAX_EXPANDED
                ):
                    raise AppError("Invalid bundle archive")
                names.add(member.name)
            manifest = archive.extractfile("manifest.json")
            if manifest is None:
                raise AppError("Missing bundle manifest")
            value = json.loads(manifest.read(65537))
            if (
                not isinstance(value, dict)
                or set(value) != {"id", "runtimeVersion", "entrypoint", "requirements", "files"}
                or value["id"] != app
                or value["runtimeVersion"] != runtime
                or not isinstance(value["files"], list)
                or any(not isinstance(name, str) for name in value["files"])
                or len(set(value["files"])) != len(value["files"])
                or "manifest.json" in value["files"]
                or set(value["files"]) | {"manifest.json"} != names
                or not isinstance(value["entrypoint"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_]*\.py", value["entrypoint"])
                or value["entrypoint"] not in value["files"]
                or (
                    value["requirements"] is not None
                    and value["requirements"] not in value["files"]
                )
            ):
                raise AppError("Bundle app or runtime contract mismatch")
            entrypoint = archive.extractfile(value["entrypoint"])
            assert entrypoint is not None
            compile(entrypoint.read(), value["entrypoint"], "exec")
            if value["requirements"] is not None:
                requirements = archive.extractfile(value["requirements"])
                assert requirements is not None
                validate_requirements(requirements.read().decode())
    except (
        OSError,
        EOFError,
        tarfile.TarError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        SyntaxError,
    ):
        raise AppError("Stored bundle failed validation") from None
    return key


def promote(
    client, bucket: str, app: str, channel: str, digest: str, runtime: str
) -> ChannelRecord:
    # Capture the predecessor before verification; a concurrent publisher must not be overwritten.
    previous, etag = read_pointer(client, bucket, app, channel)
    key = verify_bundle(client, bucket, app, digest, runtime)
    record: ChannelRecord = dict(
        version=1,
        app=app,
        channel=channel,
        revision=uuid.uuid4().hex,
        previous=previous["revision"] if previous else None,
        publishedAt=datetime.now(UTC).isoformat(),
        key=key,
        sha256=digest,
        runtimeVersion=runtime,
    )
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    prefix = channel_key(app, channel).removesuffix("current.json")
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}history/{record['revision']}.json",
        Body=encoded,
        ContentType="application/json",
        IfNoneMatch="*",
    )
    saved, _ = read_object(
        client, bucket, f"{prefix}history/{record['revision']}.json", MAX_POINTER
    )
    if saved != encoded:
        raise AppError("Channel history readback failed; channel was not advanced")
    try:
        client.put_object(
            Bucket=bucket,
            Key=channel_key(app, channel),
            Body=encoded,
            ContentType="application/json",
            **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}),
        )
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
            # A transport retry can encounter our own already-accepted conditional write.
            actual, _ = read_pointer(client, bucket, app, channel)
            if actual == record:
                return record
            raise AppError(
                "Channel changed concurrently; inspect it before retrying promotion"
            ) from None
        raise
    except BotoCoreError:
        # A timeout may follow an accepted write; never blindly repeat the promotion.
        actual, _ = read_pointer(client, bucket, app, channel)
        if actual == record:
            return record
        raise AppError(
            "Channel write outcome is uncertain; inspect the channel before retrying"
        ) from None
    actual, _ = read_pointer(client, bucket, app, channel)
    if actual != record:
        raise AppError("Channel readback differs; inspect current channel before retrying")
    return record
