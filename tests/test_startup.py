"""Exercise offline per-app dependencies and reject modified wheels."""

import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from neurwerk_pipecat import __version__
from neurwerk_pipecat.startup import prepare


class AppStartupTests(unittest.TestCase):
    def test_hashed_bundled_wheel_is_installed_only_in_app_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            (app / "wheels").mkdir(parents=True)
            (app / "main.py").write_text("import isolated_app_dependency\n")
            wheel = app / "wheels/isolated_app_dependency-1.0-py3-none-any.whl"
            metadata = "isolated_app_dependency-1.0.dist-info/"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("isolated_app_dependency.py", "VALUE = 'app-local'\n")
                archive.writestr(
                    metadata + "METADATA",
                    "Metadata-Version: 2.1\nName: isolated-app-dependency\nVersion: 1.0\n",
                )
                archive.writestr(
                    metadata + "WHEEL",
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )
                archive.writestr(metadata + "RECORD", "")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            (app / "requirements.lock").write_text(
                f"isolated-app-dependency==1.0 --hash=sha256:{digest}\n"
            )
            (app / "manifest.json").write_text(
                json.dumps(
                    {
                        "id": "test-app",
                        "runtimeVersion": __version__,
                        "entrypoint": "main.py",
                        "requirements": "requirements.lock",
                        "files": [str(p.relative_to(app)) for p in app.rglob("*") if p.is_file()],
                    }
                )
            )
            python, _ = prepare(app, root / "environment")
            result = subprocess.run(
                [str(python), "-c", "import isolated_app_dependency as d; print(d.VALUE)"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "app-local")
            with self.assertRaises(ModuleNotFoundError):
                __import__("isolated_app_dependency")
            (app / "requirements.lock").write_text(
                "isolated-app-dependency==1.0 --hash=sha256:" + "0" * 64 + "\n"
            )
            with self.assertRaises(subprocess.CalledProcessError):
                prepare(app, root / "rejected-environment")
