"""Build the app's offline Linux/CPython 3.12 dependency overlay for an exact image baseline."""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from packaging.requirements import Requirement

ENVIRONMENT: dict[str, str] = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.12",
    "platform_python_implementation": "CPython",
    "python_version": "3.12",
    "python_full_version": "3.12.12",
    "sys_platform": "linux",
    "os_name": "posix",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_release": "",
    "platform_version": "",
}


def exported(project: Path) -> dict[str, tuple[str, str]]:
    result = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-default-groups",
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--project",
            str(project),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    packages = {}
    for line in result.stdout.replace("\\\n", " ").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        spec, *hashes = line.split("--hash=")
        requirement = Requirement(spec.strip())
        if requirement.marker and not requirement.marker.evaluate(ENVIRONMENT):
            continue
        name = re.sub(r"[-_.]+", "-", requirement.name).lower()
        version = str(requirement.specifier)
        if not re.fullmatch(r"==[A-Za-z0-9][A-Za-z0-9_.+!-]*", version) or not hashes:
            raise ValueError("Expected a pinned, hashed registry dependency")
        locked = name + version + " " + " ".join("--hash=" + h.strip() for h in hashes)
        packages[name] = (version, locked)
    return packages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-project", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    baseline = exported(args.baseline_project.resolve())
    app = exported(root)
    overlay = [
        locked
        for name, (version, locked) in sorted(app.items())
        if name not in baseline or baseline[name][0] != version
    ]
    lock = root / "requirements.lock"
    lock.write_text(
        "# Linux x86_64 / CPython 3.12 overlay; regenerate with prepare_dependencies.py\n"
        + "\n".join(overlay)
        + "\n"
    )
    wheels = root / "wheels"
    wheels.mkdir(exist_ok=True)
    # Download into a fresh build directory so stale artifacts never enter the allowlist.
    build = root / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=build, prefix="wheels-") as temporary:
        downloaded = Path(temporary)
        download_wheels(lock, downloaded)
        names = sorted(path.name for path in downloaded.glob("*.whl"))
        for name in names:
            (wheels / name).write_bytes((downloaded / name).read_bytes())
    manifest = yaml.safe_load((root / "app.yaml").read_text())
    manifest["files"] = [name for name in manifest["files"] if not name.startswith("wheels/")]
    manifest["files"] += ["wheels/" + name for name in names]
    (root / "app.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    wheel_bytes = sum((wheels / name).stat().st_size for name in names)
    print(f"Prepared {len(overlay)} pinned overlays; {wheel_bytes} wheel bytes")


def download_wheels(lock: Path, wheels: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--no-deps",
            "--require-hashes",
            "--python-version",
            "3.12",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            "--platform",
            "manylinux_2_28_x86_64",
            "--dest",
            str(wheels),
            "-r",
            str(lock),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
