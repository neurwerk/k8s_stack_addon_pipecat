import io
import json
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from botocore.exceptions import ClientError

from pipecat_tooling import apps


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "apps"
    directory = root / "assistant"
    directory.mkdir(parents=True)
    (directory / "app.yaml").write_text(
        "id: assistant\nruntimeVersion: 0.2.0\nentrypoint: bot.py\nrequirements: null\n"
        "files: [bot.py]\n"
    )
    (directory / "bot.py").write_text("async def run_bot(*args, **kwargs):\n    pass\n")
    return root


@pytest.fixture
def client(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "client"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    values = repo / "values.yaml"
    values.write_text(
        "# operator comment\nunrelated: keep\npipecat:\n  enabled: true\n  bots:\n"
        "    - id: assistant\n      anonymous: false\n      title: Local edit\n"
        "    - id: other\n      enabled: true\n      script: {key: unchanged}\n"
    )
    subprocess.run(["git", "add", "values.yaml"], cwd=repo, check=True)
    return repo, values


def change_args(command: str, client: tuple[Path, Path]) -> list[str]:
    repo, values = client
    return [
        command,
        "--app",
        "assistant",
        "--client-repo",
        str(repo),
        "--values",
        str(values),
        "--client",
        "example",
        "--context",
        "example-context",
        "--namespace",
        "pipecat",
        "--yes",
    ]


def test_deterministic_allowlist_and_exact_runtime_manifest(source: Path):
    directory = source / "assistant"
    (directory / ".env").write_text("do not publish")
    first = apps.package(source, "assistant")
    (directory / "bot.py").touch()
    second = apps.package(source, "assistant")
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first.data), mode="r:gz") as archive:
        assert archive.getnames() == ["bot.py", "manifest.json"]
        manifest = archive.extractfile("manifest.json")
        assert manifest is not None
        assert json.load(manifest) == {
            "id": "assistant",
            "runtimeVersion": "0.2.0",
            "entrypoint": "bot.py",
            "requirements": None,
            "files": ["bot.py"],
        }


def test_complete_server_and_prebuilt_assets_are_packaged_without_pipeline_hook(source: Path):
    directory = source / "assistant"
    (directory / "main.py").write_text("print('app-owned server')\n")
    (directory / "frontend/dist").mkdir(parents=True)
    (directory / "frontend/dist/index.html").write_text("<main>compiled app UI</main>")
    (directory / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "assistant",
                "runtimeVersion": "0.2.0",
                "entrypoint": "main.py",
                "requirements": None,
                "files": ["main.py", "frontend/dist/index.html"],
            }
        )
    )
    bundle = apps.package(source, "assistant")
    with tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz") as archive:
        asset = archive.extractfile("frontend/dist/index.html")
        assert asset is not None
        assert asset.read() == b"<main>compiled app UI</main>"
        assert "bot.py" not in archive.getnames()


@pytest.mark.parametrize("lock", ["httpx", "httpx>=0.28", "httpx==0.28.1", "-r other.lock"])
def test_unpinned_or_indirect_startup_dependencies_are_rejected(source: Path, lock: str):
    directory = source / "assistant"
    config = yaml.safe_load((directory / "app.yaml").read_text())
    config["requirements"] = "requirements.lock"
    config["files"].append("requirements.lock")
    (directory / "app.yaml").write_text(yaml.safe_dump(config))
    (directory / "requirements.lock").write_text(lock)
    with pytest.raises(apps.AppError, match="exact versions"):
        apps.package(source, "assistant")


def test_python_secret_reference_annotation_is_not_a_yaml_secret(source: Path):
    directory = source / "assistant"
    (directory / "auth.py").write_text("class OIDC:\n    client_secret: SecretRef\n")
    assert apps.safe_file(directory, "auth.py") == b"class OIDC:\n    client_secret: SecretRef\n"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("config.py", 'client_secret = "embedded-value"\n'),
        ("config.py", 'password: str = "embedded-value"\n'),
        ("config.py", 'api_key = "embedded-value"\n'),
        ("config.yaml", "client_secret: embedded-value\n"),
        ("config.yaml", "password: embedded-value\n"),
        ("config.yaml", "api_key: embedded-value\n"),
    ],
)
def test_real_python_and_yaml_credential_values_still_rejected(source, name, content):
    directory = source / "assistant"
    (directory / name).write_text(content)
    with pytest.raises(apps.AppError, match="credential"):
        apps.safe_file(directory, name)


@pytest.mark.parametrize(
    "name", ["../outside.py", "/outside.py", ".env", "keys/key.pem", "secrets.txt"]
)
def test_unsafe_allowlist_rejected(source: Path, name: str):
    config = source / "assistant/app.yaml"
    config.write_text(
        yaml.safe_dump({"id": "assistant", "runtimeVersion": "0.2.0", "files": ["bot.py", name]})
    )
    with pytest.raises(apps.AppError):
        apps.package(source, "assistant")


def test_symlink_outside_root_and_embedded_key_rejected(source: Path, tmp_path: Path):
    bot = source / "assistant/bot.py"
    bot.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("async def run_bot(): pass")
    bot.symlink_to(outside)
    with pytest.raises(apps.AppError, match="symlinks"):
        apps.package(source, "assistant")
    bot.unlink()
    bot.write_text('api_key = "not-a-real-api-key"\nasync def run_bot(): pass')
    with pytest.raises(apps.AppError, match="credential"):
        apps.package(source, "assistant")


def test_publish_failure_never_updates_values(source: Path, client, monkeypatch):
    original = client[1].read_bytes()
    remote = Mock()
    remote.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "sensitive response"}}, "PutObject"
    )
    monkeypatch.setattr(apps.boto3, "client", lambda *args, **kwargs: remote)
    args = change_args("publish", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
        "--secret-name",
        "app-s3",
    ]
    assert apps.main(args) == 1
    assert client[1].read_bytes() == original
    remote.close.assert_called_once()


def test_corrupted_existing_s3_object_leaves_values_unchanged(source: Path, client, monkeypatch):
    original = client[1].read_bytes()
    remote = Mock()
    remote.put_object.side_effect = ClientError(
        {"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject"
    )
    body = io.BytesIO(b"corrupted existing object")
    remote.get_object.return_value = {"Body": body}
    monkeypatch.setattr(apps.boto3, "client", lambda *args, **kwargs: remote)
    args = change_args("update", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
        "--secret-name",
        "app-s3",
    ]
    assert apps.main(args) == 1
    assert client[1].read_bytes() == original
    assert body.closed
    remote.close.assert_called_once()


def test_immutable_publish_and_targeted_values_update(source: Path, client, monkeypatch):
    bundle = apps.package(source, "assistant")
    remote = Mock()
    remote.put_object.side_effect = ClientError(
        {"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject"
    )
    remote.get_object.return_value = {"Body": io.BytesIO(bundle.data)}
    monkeypatch.setattr(apps.boto3, "client", lambda *args, **kwargs: remote)
    before = yaml.safe_load(client[1].read_text())
    args = change_args("update", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
        "--secret-name",
        "app-s3",
    ]
    assert apps.main(args) == 0
    after = yaml.safe_load(client[1].read_text())
    script = after["pipecat"]["bots"][0].pop("script")
    assert after == before
    assert script["sha256"] == bundle.sha256
    assert script["key"] == f"pipecat/assistant/{bundle.sha256}.tar.gz"
    assert remote.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    assert "# operator comment" in client[1].read_text()


def test_disable_retains_bot_and_other_git_changes(client):
    original = client[1].read_text()
    before = yaml.safe_load(client[1].read_text())
    assert apps.main(change_args("disable", client)) == 0
    after = yaml.safe_load(client[1].read_text())
    assert after["pipecat"]["bots"][0].pop("enabled") is False
    assert after == before
    assert client[1].read_text() == original.replace(
        "    - id: other", "      enabled: false\n    - id: other"
    )


def test_existing_script_update_preserves_unrelated_yaml_bytes(client):
    repo, path = client
    original = path.read_text().replace(
        "      title: Local edit",
        "      script:\n        enabled: true\n        key: old\n      title: Local edit",
    )
    path.write_text(original)
    selected = apps.ClientValues.load(repo, path, "assistant")
    selected.bot["script"]["key"] = "new"
    selected.write()
    assert path.read_text() == original.replace(
        "enabled: true\n        key: old\n      title:", "{enabled: true, key: new}\n      title:"
    )


def test_upload_endpoint_separate_from_runtime_and_rook_secret_keys(source, client, monkeypatch):
    repo, values = client
    selected = apps.ClientValues.load(repo, values, "assistant")
    runtime_endpoint = "http://storage.object-store.svc:80"
    selected.bot["script"] = {
        "endpoint": runtime_endpoint,
        "secretName": "existing-reader",
        "accessKeyKey": "AccessKey",
        "secretKeyKey": "SecretKey",
    }
    selected.write()
    bundle = apps.package(source, "assistant")
    remote = Mock()
    remote.get_object.return_value = {"Body": io.BytesIO(bundle.data)}
    factory = Mock(return_value=remote)
    monkeypatch.setattr(apps.boto3, "client", factory)
    args = change_args("update", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
    ]
    assert apps.main(args) == 0
    script = yaml.safe_load(values.read_text())["pipecat"]["bots"][0]["script"]
    assert script["endpoint"] == runtime_endpoint
    assert script["secretName"] == "existing-reader"
    assert script["accessKeyKey"] == "AccessKey"
    assert script["secretKeyKey"] == "SecretKey"
    assert factory.call_args.kwargs["endpoint_url"] == "https://s3.example.com"


@pytest.mark.parametrize(
    "endpoint,opt_in,allowed",
    [
        ("http://127.0.0.1:8080", False, False),
        ("http://127.0.0.1:8080", True, True),
        ("http://localhost:8080", True, True),
        ("http://[::1]:8080", True, True),
        ("http://s3.example.com", True, False),
        ("http://127.0.0.1.example.com", True, False),
        ("https://user:password@example.com", False, False),
    ],
)
def test_http_upload_requires_explicit_loopback_opt_in(endpoint, opt_in, allowed):
    if allowed:
        apps.endpoint_url(endpoint, local_http=opt_in)
    else:
        with pytest.raises(apps.AppError):
            apps.endpoint_url(endpoint, local_http=opt_in)


def test_publish_refuses_implicit_secret_delivery_reference_change(source, client, monkeypatch):
    repo, values = client
    selected = apps.ClientValues.load(repo, values, "assistant")
    selected.bot["script"] = {"secretName": "existing-reader"}
    selected.write()
    original = values.read_bytes()
    publisher = Mock()
    monkeypatch.setattr(apps, "publish", publisher)
    args = change_args("update", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "https://s3.example.com",
        "--bucket",
        "apps",
        "--secret-name",
        "different-reader",
    ]
    assert apps.main(args) == 1
    publisher.assert_not_called()
    assert values.read_bytes() == original


def test_first_port_forward_publish_requires_runtime_endpoint(source, client, monkeypatch):
    bundle = apps.package(source, "assistant")
    remote = Mock()
    remote.get_object.return_value = {"Body": io.BytesIO(bundle.data)}
    factory = Mock(return_value=remote)
    monkeypatch.setattr(apps.boto3, "client", factory)
    args = change_args("publish", client) + [
        "--apps-dir",
        str(source),
        "--endpoint",
        "http://127.0.0.1:8080",
        "--bucket",
        "apps",
        "--secret-name",
        "existing-reader",
        "--allow-local-http",
    ]
    original = client[1].read_bytes()
    assert apps.main(args) == 1
    assert client[1].read_bytes() == original
    factory.assert_not_called()
    runtime_endpoint = "http://storage.object-store.svc:80"
    assert apps.main(args + ["--runtime-endpoint", runtime_endpoint]) == 0
    script = yaml.safe_load(client[1].read_text())["pipecat"]["bots"][0]["script"]
    assert script["endpoint"] == runtime_endpoint
    assert factory.call_args.kwargs["endpoint_url"] == "http://127.0.0.1:8080"


def test_reject_outside_values_and_concurrent_edit(client, tmp_path: Path):
    repo, values = client
    outside = tmp_path / "other.yaml"
    outside.write_text(values.read_text())
    with pytest.raises(apps.AppError, match="inside"):
        apps.ClientValues.load(repo, outside, "assistant")
    selected = apps.ClientValues.load(repo, values, "assistant")
    values.write_text(values.read_text() + "# newer edit\n")
    with pytest.raises(apps.AppError, match="changed"):
        selected.write()
    assert values.read_text().endswith("# newer edit\n")


def test_help_status_logs_do_not_mutate(client, monkeypatch):
    original = client[1].read_bytes()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return "selected app only\n"

    monkeypatch.setattr(apps, "run", run)
    assert apps.main([]) == 0
    with pytest.raises(SystemExit) as exit_info:
        apps.main(["--help"])
    assert exit_info.value.code == 0
    for command in ("status", "logs"):
        assert (
            apps.main(
                [
                    command,
                    "--app",
                    "assistant",
                    "--context",
                    "explicit-context",
                    "--client",
                    "example",
                    "--namespace",
                    "pipecat",
                ]
            )
            == 0
        )
    assert len(commands) == 2
    assert all(command[0] == "kubectl" and "explicit-context" in command for command in commands)
    assert "--tail=100" in commands[1]
    assert client[1].read_bytes() == original
