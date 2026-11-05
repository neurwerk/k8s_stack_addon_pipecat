"""Checksum, extraction bounds and atomic publication of complete trusted apps."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from neurwerk_pipecat import __version__
from neurwerk_pipecat.bundle import bundle_reference, extract


class BundleTests(unittest.TestCase):
    def test_manifest_selected_chatbot_modules_import(self):
        app = Path(__file__).resolve().parents[1] / "apps/simple-chatbot"
        manifest = yaml.safe_load((app / "app.yaml").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in manifest["files"]:
                if name.endswith(".py"):
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(app / name, target)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import pathlib, runpy, sys; "
                    "root = pathlib.Path.cwd().resolve(); sys.path.insert(0, str(root)); "
                    "runpy.run_path('server.py', run_name='bundle_import_check'); "
                    "import simple_chatbot.bot; "
                    "assert all(pathlib.Path(module.__file__).resolve().is_relative_to(root) "
                    "for name, module in sys.modules.items() "
                    "if name == 'simple_chatbot' or name.startswith('simple_chatbot.'))",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def archive(self, root, entries):
        path = root / "bundle.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for info, value in entries:
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
        return path

    def files(self, **changes):
        manifest = {
            "id": "assistant",
            "runtimeVersion": __version__,
            "entrypoint": "main.py",
            "requirements": None,
            "files": ["main.py"],
        }
        manifest.update(changes)
        return [
            (tarfile.TarInfo("manifest.json"), json.dumps(manifest).encode()),
            (tarfile.TarInfo("main.py"), b"print('app-owned server')\n"),
        ]

    def test_verified_bundle_published_and_invalid_manifest_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.archive(root, self.files())
            extract(path, root / "accepted", hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertTrue((root / "accepted/main.py").is_file())
            with self.assertRaisesRegex(ValueError, "identity"):
                extract(
                    path,
                    root / "wrong-app",
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    expected_app="another-app",
                )
            self.assertFalse((root / "wrong-app").exists())
            for changes in (
                {"runtimeVersion": "unsupported"},
                {"runtimeVersion": "0.1.0"},
                {"entrypoint": "../main.py"},
                {"files": []},
                {"requirements": "missing.lock"},
            ):
                path = self.archive(root, self.files(**changes))
                with self.assertRaises(ValueError):
                    extract(path, root / "rejected", hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertFalse((root / "rejected").exists())

    def test_channel_reference_binds_policy_and_preserves_bootstrap(self):
        digest = "a" * 64
        release = {
            "app": "assistant",
            "endpoint": "https://s3.example.com",
            "bucket": "apps",
            "channel": "dev",
            "runtimeVersion": __version__,
            "sha256": digest,
            "key": f"pipecat/assistant/{digest}.tar.gz",
        }
        env = {
            "S3_KEY": "bootstrap",
            "BUNDLE_SHA256": "b" * 64,
            "BUNDLE_APP_ID": "assistant",
            "BUNDLE_CHANNEL": "dev",
            "BUNDLE_RUNTIME_VERSION": __version__,
        }
        with patch.dict(os.environ, env, clear=True):

            def resolve():
                return bundle_reference(release["endpoint"], release["bucket"])

            bootstrap = (env["S3_KEY"], env["BUNDLE_SHA256"])
            self.assertEqual(resolve(), bootstrap)
            os.environ["BUNDLE_CHANNEL_RELEASE"] = json.dumps(release)
            self.assertEqual(resolve(), (release["key"], digest))
            for field in ("app", "endpoint", "bucket", "channel", "runtimeVersion"):
                with self.subTest(field=field):
                    os.environ["BUNDLE_CHANNEL_RELEASE"] = json.dumps({**release, field: "old"})
                    self.assertEqual(resolve(), bootstrap)
            for invalid in (
                "[",
                "[]",
                "x" * 8193,
                json.dumps({**release, "key": f"pipecat/other/{digest}.tar.gz"}),
                json.dumps({**release, "sha256": "invalid"}),
            ):
                os.environ["BUNDLE_CHANNEL_RELEASE"] = invalid
                with self.assertRaises(ValueError):
                    resolve()
            del os.environ["BUNDLE_CHANNEL"]
            self.assertEqual(resolve(), bootstrap)

    def test_hash_checked_before_parsing_and_unsafe_members_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.archive(root, self.files())
            with patch("neurwerk_pipecat.bundle.tarfile.open") as parser:
                with self.assertRaises(ValueError):
                    extract(path, root / "out", "0" * 64)
                parser.assert_not_called()
            members = [
                tarfile.TarInfo("../escape"),
                tarfile.TarInfo("/absolute"),
                tarfile.TarInfo("main.py"),
            ]
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "main.py"
            members.append(link)
            for member in members:
                path = self.archive(root, self.files() + [(member, b"bad")])
                with self.assertRaises(ValueError):
                    extract(path, root / "out", hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertFalse((root / "out").exists())

    def test_file_and_expansion_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.archive(root, self.files())
            for constant in ("MAX_FILES", "MAX_EXPANDED", "MAX_ARCHIVE"):
                with patch("neurwerk_pipecat.bundle." + constant, 1):
                    with self.assertRaises(ValueError):
                        extract(path, root / "out", hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertFalse((root / "out").exists())
