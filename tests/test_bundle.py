"""Checksum, extraction bounds and atomic publication of complete trusted apps."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neurwerk_pipecat import __version__
from neurwerk_pipecat.bundle import extract


class BundleTests(unittest.TestCase):
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
