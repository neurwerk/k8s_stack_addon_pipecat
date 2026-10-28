"""Init-container loader for explicitly trusted private Python scripts, not a sandbox."""

import gzip
import hashlib
import importlib.util
import inspect
import json
import os
import re
import tarfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import boto3
from botocore.config import Config

from neurwerk_pipecat import __version__

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILES = 128


def extract(archive: Path, target: Path, expected_hash: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        raise ValueError("Expected exact SHA-256")
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Archive too large")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("Bundle hash mismatch")
    # Bound decompression BEFORE tarfile parses PAX/GNU extension headers; those can
    # otherwise allocate memory before a regular member's size can be inspected.
    with TemporaryDirectory() as temporary:
        raw = Path(temporary) / "bundle.tar"
        total = 0
        with gzip.open(archive, "rb") as compressed, raw.open("xb") as output:
            while chunk := compressed.read(65536):
                total += len(chunk)
                if total > MAX_EXPANDED + MAX_FILES * 2048:
                    raise ValueError("Decompressed archive limit exceeded")
                output.write(chunk)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and any(target.iterdir()):
            raise ValueError("Target must be empty")
        with TemporaryDirectory(dir=target.parent, prefix=".bundle-") as staging:
            staged = Path(staging) / "verified"
            staged.mkdir()
            extract_tar(raw, staged)
            validate_manifest(staged)
            # Publish only a fully validated bundle, on the destination filesystem.
            staged.replace(target)


def extract_tar(archive: Path, target: Path) -> None:
    # Never extract until ALL headers and the complete expanded-size bound are checked.
    with tarfile.open(archive, "r:") as source:
        files = []
        names = set()
        size = 0
        for member in source:
            name = PurePosixPath(member.name)
            if (
                not member.isfile()
                or member.sparse is not None
                or name.is_absolute()
                or ".." in name.parts
                or "\\" in member.name
                or not name.parts
                or any(part.startswith(".") for part in name.parts)
                or str(name) in names
            ):
                raise ValueError("Unsafe bundle member")
            names.add(str(name))
            size += member.size
            files.append(member)
            if len(files) > MAX_FILES or size > MAX_EXPANDED:
                raise ValueError("Expanded bundle limit exceeded")
        if target.exists() and any(target.iterdir()):
            raise ValueError("Target must be empty")
        for member in files:
            destination = target / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            assert stream
            with destination.open("xb") as output:
                while chunk := stream.read(65536):
                    output.write(chunk)
            destination.chmod(0o444)
    validate_manifest(target)


def validate_manifest(target: Path) -> Path:
    manifest = json.loads((target / "manifest.json").read_text())
    if not isinstance(manifest, dict) or set(manifest) != {"runtimeVersion", "entrypoint"}:
        raise ValueError("Invalid script manifest fields")
    if manifest["runtimeVersion"] != __version__:
        raise ValueError("Incompatible script runtime")
    entrypoint = manifest["entrypoint"]
    if not isinstance(entrypoint, str) or not re.fullmatch(r"[a-z][a-z0-9_]*\.py", entrypoint):
        raise ValueError("Entrypoint must be a root-level Python file")
    path = target / entrypoint
    if not path.is_file() or path.is_symlink():
        raise ValueError("Missing script entrypoint")
    return path


def load_script(target: Path):
    path = validate_manifest(target)
    spec = importlib.util.spec_from_file_location("trusted_bot", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not inspect.iscoroutinefunction(getattr(module, "run_bot", None)):
        raise ValueError("Script must export async run_bot")
    return module.run_bot


def load_bundle() -> None:
    target = Path(os.environ.get("BUNDLE_TARGET", "/scripts/bundle"))
    endpoint = os.environ["S3_ENDPOINT"]
    if not endpoint.startswith(("https://", "http://")):
        raise ValueError("Invalid S3 endpoint")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
    )
    result = client.get_object(Bucket=os.environ["S3_BUCKET"], Key=os.environ["S3_KEY"])
    try:
        if result["ContentLength"] > MAX_ARCHIVE:
            raise ValueError("Archive too large")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "bundle.tar.gz"
            size = 0
            with path.open("xb") as output:
                for chunk in result["Body"].iter_chunks(65536):
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise ValueError("Archive too large")
                    output.write(chunk)
            extract(path, target, os.environ["BUNDLE_SHA256"])
    finally:
        result["Body"].close()
        client.close()


def main() -> None:
    try:
        load_bundle()
    except Exception:
        # S3 errors may include request-signing details. Do not print service payloads.
        raise SystemExit("Private script initialization failed") from None
