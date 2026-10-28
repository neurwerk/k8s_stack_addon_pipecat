"""Prepare one private app environment, then replace the launcher with its server."""

import json
import os
import subprocess
import sysconfig
import venv
from pathlib import Path

from neurwerk_pipecat.bundle import validate_manifest


def prepare(target: Path, environment: Path) -> tuple[Path, Path]:
    entrypoint = validate_manifest(target)
    manifest = json.loads((target / "manifest.json").read_text())
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / "bin/python"
    # Inherit the locked image venv, not the base interpreter's site-packages.
    site = Path(
        sysconfig.get_path(
            "purelib",
            vars={
                "base": str(environment),
                "platbase": str(environment),
            },
        )
    )
    (site / "runtime-baseline.pth").write_text(sysconfig.get_path("purelib") + "\n")
    if manifest["requirements"] is not None:
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-index",
                "--find-links",
                str(target / "wheels"),
                "--only-binary=:all:",
                "--no-deps",
                "--require-hashes",
                "--ignore-installed",
                "--no-cache-dir",
                "-r",
                str(target / manifest["requirements"]),
            ],
            check=True,
            timeout=120,
            capture_output=True,
        )
    subprocess.run(
        [str(python), "-m", "pip", "--isolated", "check"],
        check=True,
        timeout=30,
        capture_output=True,
    )
    return python, entrypoint


def main() -> None:
    target = Path(os.environ.get("BUNDLE_TARGET", "/scripts/bundle")).resolve()
    environment = Path(os.environ.get("APP_ENVIRONMENT", "/tmp/app-venv")).resolve()
    try:
        python, entrypoint = prepare(target, environment)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("App environment preparation failed") from None
    os.chdir(target)
    os.environ["VIRTUAL_ENV"] = str(environment)
    os.environ["PATH"] = str(environment / "bin") + os.pathsep + os.environ.get("PATH", "")
    os.execv(str(python), [str(python), str(entrypoint)])
