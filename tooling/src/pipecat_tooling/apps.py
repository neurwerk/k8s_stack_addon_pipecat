"""Manage trusted Pipecat app bundles and explicit client Git working-tree values."""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from k8s_stack_tooling.gateway_catalog.values import read_values
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILES = 128
ID = re.compile(r"[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?")
SECRET_NAMES = {"credentials", "secrets", "secret", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".env", ".kubeconfig"}


class AppError(ValueError):
    """Safe operator-facing failure, without supplied values or service responses."""


def identifier(value: str) -> str:
    if not ID.fullmatch(value) or value in {"turn", "product-values"}:
        raise AppError("Invalid app ID")
    return value


def run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AppError("External command failed; inspect the selected configuration separately")
    return result.stdout


def app_path(root: Path, app: str) -> Path:
    identifier(app)
    root = root.resolve(strict=True)
    selected = root / app
    if selected.is_symlink() or not selected.is_dir() or selected.resolve().parent != root:
        raise AppError("App must be a direct, non-symlink child of --apps-dir")
    return selected


def scan(root: Path) -> list[str]:
    return sorted(
        path.name
        for path in root.resolve(strict=True).iterdir()
        if ID.fullmatch(path.name)
        and path.name not in {"turn", "product-values"}
        and path.is_dir()
        and not path.is_symlink()
        and (path / "app.yaml").is_file()
    )


def safe_file(root: Path, name: str) -> bytes:
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or str(path) != name
        or "\\" in name
        or any(
            part.startswith(".")
            or part in {"..", "__pycache__", "node_modules", "venv"}
            or part.lower() in SECRET_NAMES
            or Path(part).suffix.lower() in SECRET_SUFFIXES
            or "secret" in part.lower()
            or "credential" in part.lower()
            for part in path.parts
        )
    ):
        raise AppError("Unsafe or credential-like bundle path")
    source = root
    for part in path.parts:
        source = source / part
        if source.is_symlink():
            raise AppError("Bundle symlinks are forbidden")
    if not source.resolve(strict=True).is_relative_to(root.resolve()):
        raise AppError("Bundle path escapes app root")
    if not source.is_file() or source.stat().st_nlink != 1:
        raise AppError("Bundle entries must be ordinary, non-hardlinked files")
    with source.open("rb") as stream:
        data = stream.read(MAX_EXPANDED + 1)
    if len(data) > MAX_EXPANDED:
        raise AppError("Bundle file is too large")
    # Mask only AST-confirmed value-less annotations for the YAML-value heuristic.
    # Assignments and the raw embedded-key/string checks remain fully scanned.
    scanned = data
    if path.suffix == ".py":
        lines = data.splitlines(keepends=True)
        for node in ast.walk(ast.parse(data, filename=name)):
            if isinstance(node, ast.AnnAssign) and node.value is None:
                for index in range(node.lineno - 1, node.end_lineno or node.lineno):
                    lines[index] = b"\n"
        scanned = b"".join(lines)
    # Fail closed for common accidentally embedded credentials, not a secret scanner.
    if re.search(
        rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[A-Z0-9]{16}\b|"
        rb"(?i:aws_secret_access_key|api_key|password|client_secret)['\"]?\s*[:=]\s*"
        rb"['\"][^'\"\r\n]+['\"]",
        data,
    ) or re.search(
        rb"(?im:^\s*(?:aws_secret_access_key|api_key|password|client_secret)\s*:\s*\S+)",
        scanned,
    ):
        raise AppError("Possible embedded credential in bundle")
    return data


@dataclass(frozen=True)
class Bundle:
    data: bytes
    sha256: str
    runtime_version: str


def package(root: Path, app: str) -> Bundle:
    directory = app_path(root, app)
    config = YAML(typ="safe").load(safe_file(directory, "app.yaml"))
    if not isinstance(config, dict) or set(config) != {
        "id",
        "runtimeVersion",
        "files",
        "entrypoint",
        "requirements",
    }:
        raise AppError("app.yaml requires id, runtimeVersion, files, entrypoint and requirements")
    version = config["runtimeVersion"]
    files = config["files"]
    if (
        config["id"] != app
        or not isinstance(version, str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]*)", version)
    ):
        raise AppError("App ID must match its folder and runtimeVersion must be exact")
    if (
        not isinstance(files, list)
        or not files
        or len(files) >= MAX_FILES
        or any(not isinstance(name, str) for name in files)
        or len(set(files)) != len(files)
        or any(name in {"app.yaml", "manifest.json"} for name in files)
    ):
        raise AppError("files must explicitly list all app artifacts, without duplicates")
    entrypoint = config["entrypoint"]
    requirements = config["requirements"]
    if (
        not isinstance(entrypoint, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]*\.py", entrypoint)
        or entrypoint not in files
    ):
        raise AppError("entrypoint must name an allowlisted root Python server file")
    if requirements is not None and (
        not isinstance(requirements, str) or requirements not in files
    ):
        raise AppError("requirements must name an allowlisted dependency lock or be null")
    payload = {}
    total = 0
    for name in files:
        content = safe_file(directory, name)
        total += len(content)
        if total > MAX_EXPANDED:
            raise AppError("Expanded bundle limit exceeded")
        payload[name] = content
    compile(payload[entrypoint], entrypoint, "exec")
    if requirements is not None:
        for line in payload[requirements].decode().replace("\\\n", " ").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*"
                r"(?:\s+--hash=sha256:[a-f0-9]{64})+",
                line,
            ):
                raise AppError("Dependencies require exact versions and SHA-256 hashes")
    payload["manifest.json"] = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    if sum(map(len, payload.values())) > MAX_EXPANDED:
        raise AppError("Expanded bundle limit exceeded")
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content in sorted(payload.items()):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o444
                archive.addfile(member, io.BytesIO(content))
    data = output.getvalue()
    if len(data) > MAX_ARCHIVE:
        raise AppError("Compressed bundle limit exceeded")
    return Bundle(data, hashlib.sha256(data).hexdigest(), version)


def endpoint_url(endpoint: str, *, local_http: bool = False, runtime: bool = False) -> None:
    if not isinstance(endpoint, str):
        raise AppError("S3 endpoint must be a URL string")
    parsed = urlsplit(endpoint)
    allowed_http = runtime or (local_http and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
    if (
        (parsed.scheme != "https" and not (parsed.scheme == "http" and allowed_http))
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AppError("Use HTTPS for uploads; loopback HTTP requires --allow-local-http")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise AppError("Invalid S3 endpoint port")


def publish(
    bundle: Bundle, app: str, endpoint: str, bucket: str, *, allow_local_http: bool = False
) -> str:
    endpoint_url(endpoint, local_http=allow_local_http)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise AppError("Invalid S3 bucket")
    key = f"pipecat/{identifier(app)}/{bundle.sha256}.tar.gz"
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
    try:
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=bundle.data,
                ContentType="application/gzip",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise AppError("S3 publish failed; desired state was not updated") from None
        # Verify bytes, including when a previous invocation already published this key.
        result = client.get_object(Bucket=bucket, Key=key)
        try:
            data = result["Body"].read(MAX_ARCHIVE + 1)
        finally:
            result["Body"].close()
        if hashlib.sha256(data).hexdigest() != bundle.sha256:
            raise AppError("Published object verification failed; desired state was not updated")
    except (BotoCoreError, ClientError):
        raise AppError("S3 publish failed; desired state was not updated") from None
    finally:
        client.close()
    return key


@dataclass
class ClientValues:
    path: Path
    original: str
    document: Any
    bot: Any
    yaml: Any

    def changed_text(self) -> str:
        """Replace only changed bot fields, leaving every unrelated byte intact."""
        tree = yaml.compose(self.original)

        def child(node: Any, key: str) -> Any:
            return next(value for field, value in node.value if field.value == key)

        rows = child(child(tree, "pipecat"), "bots")
        node = next(row for row in rows.value if child(row, "id").value == self.bot["id"])
        output = io.StringIO()
        self.yaml.dump(self.document, output)
        changed = next(
            row
            for row in yaml.safe_load(output.getvalue())["pipecat"]["bots"]
            if row["id"] == self.bot["id"]
        )
        previous = next(
            row
            for row in yaml.safe_load(self.original)["pipecat"]["bots"]
            if row["id"] == self.bot["id"]
        )
        if node.flow_style:
            # An inline bot is a single selected value; other bot bytes remain untouched.
            replacement = yaml.safe_dump(changed, default_flow_style=True, width=1000000).strip()
            return (
                self.original[: node.start_mark.index]
                + replacement
                + self.original[node.end_mark.index :]
            )
        edits = []
        fields = {key.value: value for key, value in node.value}
        for key, value in changed.items():
            if key in previous and previous[key] == value:
                continue
            replacement = yaml.safe_dump(value, default_flow_style=True, width=1000000).strip()
            replacement = replacement.removesuffix("\n...")
            if key in fields:
                old = fields[key]
                if (
                    old.end_mark.line > old.start_mark.line
                    and old.end_mark.column <= node.start_mark.column
                ):
                    replacement += "\n" + " " * old.end_mark.column
                edits.append((old.start_mark.index, old.end_mark.index, replacement))
            else:
                insertion = node.end_mark.index - node.end_mark.column
                prefix = "" if self.original[:insertion].endswith("\n") else "\n"
                edits.append(
                    (
                        insertion,
                        insertion,
                        prefix + " " * node.start_mark.column + key + ": " + replacement + "\n",
                    )
                )
        text = self.original
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        if yaml.safe_load(text) != yaml.safe_load(output.getvalue()):
            raise AppError("Cannot safely edit this YAML layout; use a simple bot mapping")
        return text

    @classmethod
    def load(cls, repo: Path, values: Path, app: str) -> ClientValues:
        identifier(app)
        repo = repo.resolve(strict=True)
        if Path(run(["git", "rev-parse", "--show-toplevel"], cwd=repo).strip()).resolve() != repo:
            raise AppError("--client-repo must be the explicit Git repository root")
        if values.is_symlink():
            raise AppError("Product values must not be a symlink")
        values = values.resolve(strict=True)
        if not values.is_relative_to(repo) or ".git" in values.relative_to(repo).parts:
            raise AppError("Product values must be inside the selected client Git repository")
        run(["git", "ls-files", "--error-unmatch", "--", str(values.relative_to(repo))], cwd=repo)
        original = values.read_text()
        if any(isinstance(token, yaml.AliasToken) for token in yaml.scan(original)):
            raise AppError("Values aliases are not supported for targeted app edits")
        # Reuse the shared values reader; round-trip parsing additionally rejects duplicates.
        read_values([values])
        parser = YAML()
        parser.preserve_quotes = True
        document = parser.load(original)
        product = document.get("pipecat")
        if not isinstance(product, dict) or not isinstance(product.get("bots"), list):
            raise AppError("Explicit product values must contain pipecat.bots")
        rows = product["bots"]
        if any(not isinstance(row, dict) for row in rows):
            raise AppError("Invalid bot catalog")
        ids = [row.get("id") for row in rows]
        if any(not isinstance(value, str) for value in ids) or len(set(ids)) != len(ids):
            raise AppError("Invalid or duplicate bot IDs")
        matches = [row for row in rows if row.get("id") == app]
        if len(matches) != 1:
            raise AppError(
                "First add one reviewed pipecat.bots entry with this app ID to --values. "
                "Configure OIDC, access policy and credential delivery in the client repository, "
                "validate with pipecat-validate, then publish. No client policy is generated."
            )
        return cls(values, original, document, matches[0], parser)

    def write(self) -> None:
        text = self.changed_text()
        # Refuse overwriting an edit made while packaging/publishing/confirming.
        if self.path.read_text() != self.original:
            raise AppError("Product values changed during this operation; retry after review")
        descriptor, temporary = tempfile.mkstemp(prefix=".pipecat-apps-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, self.path.stat().st_mode & 0o777)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def confirm(args: argparse.Namespace, action: str) -> None:
    print(f"{action}: app={args.app} client={args.client} context={args.context}")
    if not args.yes:
        if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() != "y":
            raise AppError("Cancelled; use --yes for an explicitly approved noninteractive change")


def cluster(args: argparse.Namespace) -> None:
    if args.context.startswith("-") or not args.context.strip():
        raise AppError("Explicit context required")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.namespace):
        raise AppError("Invalid namespace")
    command = [
        "kubectl",
        "--context",
        args.context,
        "--namespace",
        args.namespace,
        "--request-timeout=30s",
    ]
    selector = (
        f"app.kubernetes.io/name=pipecat,app.kubernetes.io/instance=pipecat-{identifier(args.app)}"
    )
    if args.command == "logs":
        if not 1 <= args.tail <= 1000:
            raise AppError("--tail must be between 1 and 1000")
        command += [
            "logs",
            "-l",
            selector,
            "-c",
            "bot",
            f"--tail={args.tail}",
            "--since=1h",
            "--prefix",
        ]
    else:
        command += ["get", "deployments,pods", "-l", selector]
    print(run(command), end="")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=__doc__,
        epilog="First use: create the app folder and reviewed client bot/identity/credential "
        "settings; "
        "validate with pipecat-validate, then publish. Commit/push the reviewed Git diff manually.",
    )
    commands = result.add_subparsers(dest="command")
    for name in (
        "interactive",
        "list",
        "validate",
        "publish",
        "update",
        "enable",
        "disable",
        "status",
        "logs",
    ):
        command = commands.add_parser(name)
        if name in {"interactive", "list", "validate", "publish", "update"}:
            command.add_argument("--apps-dir", type=Path, required=True)
        if name not in {"interactive", "list"}:
            command.add_argument("--app", required=True)
        if name in {"interactive", "publish", "update", "enable", "disable", "status", "logs"}:
            command.add_argument(
                "--client", required=True, help="Explicit operator-selected client identity"
            )
            command.add_argument("--context", required=True)
            command.add_argument("--namespace", required=True)
        if name in {"interactive", "publish", "update", "enable", "disable"}:
            command.add_argument("--client-repo", type=Path, required=True)
            command.add_argument(
                "--values", type=Path, required=True, help="Explicit product values file"
            )
            command.add_argument("--yes", action="store_true")
        if name in {"interactive", "publish", "update"}:
            command.add_argument(
                "--endpoint", required=name != "interactive", help="Workstation S3 upload endpoint"
            )
            command.add_argument(
                "--runtime-endpoint",
                help="Runtime S3 endpoint; defaults to existing script.endpoint, else --endpoint",
            )
            command.add_argument(
                "--allow-local-http",
                action="store_true",
                help="Allow HTTP uploads to loopback only",
            )
            command.add_argument("--bucket", required=name != "interactive")
            command.add_argument(
                "--secret-name",
                help="Runtime S3 Secret name for first publish; existing reference is preserved",
            )
        if name in {"interactive", "logs"}:
            command.add_argument("--tail", type=int, default=100)
    return result


def interactive(args: argparse.Namespace) -> None:
    if not sys.stdin.isatty():
        raise AppError("Interactive mode requires a terminal; select a subcommand instead")
    apps = scan(args.apps_dir)
    if not apps:
        raise AppError("No apps found in --apps-dir")
    for index, name in enumerate(apps, 1):
        print(f"{index}. {name}")
    choice = input("Select app number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(apps):
        raise AppError("Invalid app selection")
    args.app = apps[int(choice) - 1]
    actions = ["validate", "publish", "update", "enable", "disable", "status", "logs"]
    print("Actions: " + ", ".join(actions))
    args.command = input("Action: ").strip()
    if args.command not in actions:
        raise AppError("Invalid action")
    if args.command in {"publish", "update"}:
        for field in ("endpoint", "bucket"):
            if not getattr(args, field):
                setattr(args, field, input(f"{field}: ").strip())


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 0
    try:
        if args.command == "interactive":
            interactive(args)
        if args.command == "list":
            print("\n".join(scan(args.apps_dir)))
            return 0
        identifier(args.app)
        if args.command == "validate":
            bundle = package(args.apps_dir, args.app)
            print(f"Valid: runtimeVersion={bundle.runtime_version} sha256={bundle.sha256}")
            return 0
        if args.command in {"status", "logs"}:
            cluster(args)
            return 0
        values = ClientValues.load(args.client_repo, args.values, args.app)
        if not isinstance(values.bot.get("script", {}), dict):
            raise AppError("Existing script settings must be a mapping")
        bundle = package(args.apps_dir, args.app) if args.command in {"publish", "update"} else None
        if bundle:
            existing = values.bot.get("script", {})
            secret = existing.get("secretName") or args.secret_name
            if args.secret_name and existing.get("secretName") not in {None, args.secret_name}:
                raise AppError(
                    "Existing S3 Secret reference differs; change delivery settings manually"
                )
            if not secret and sys.stdin.isatty() and not args.yes:
                secret = input("Existing runtime S3 Secret name: ").strip()
            if not isinstance(secret, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", secret
            ):
                raise AppError(
                    "First publish requires --secret-name for an existing delivered S3 Secret"
                )
            runtime_endpoint = args.runtime_endpoint or existing.get("endpoint") or args.endpoint
            if (
                not args.runtime_endpoint
                and not existing.get("endpoint")
                and urlsplit(args.endpoint).scheme == "http"
            ):
                raise AppError(
                    "First HTTP upload requires an explicit in-cluster --runtime-endpoint"
                )
            endpoint_url(runtime_endpoint, runtime=True)
            endpoint_url(args.endpoint, local_http=args.allow_local_http)
        confirm(args, args.command)
        if bundle:
            key = publish(
                bundle, args.app, args.endpoint, args.bucket, allow_local_http=args.allow_local_http
            )
            script = values.bot.setdefault("script", {})
            if not isinstance(script, dict):
                raise AppError("Existing script settings must be a mapping")
            script.update(
                enabled=True,
                endpoint=runtime_endpoint,
                bucket=args.bucket,
                key=key,
                sha256=bundle.sha256,
                secretName=secret,
            )
        else:
            values.bot["enabled"] = args.command == "enable"
        values.write()
        print(
            "Updated product values only. Review the Git diff, commit and push explicitly; "
            "Flux reconciles it."
        )
        return 0
    except AppError as error:
        print(str(error), file=sys.stderr)
    except (
        OSError,
        ValueError,
        SyntaxError,
        TypeError,
        KeyError,
        BotoCoreError,
        ClientError,
        yaml.YAMLError,
        YAMLError,
        subprocess.SubprocessError,
    ):
        print(
            "Invalid input or local operation failed; supplied values are hidden", file=sys.stderr
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
