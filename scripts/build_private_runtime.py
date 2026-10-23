"""Build a local Windows development payload from verified runtime + pinned packages.

No download, signing, installation, or copying of a development venv. This is not
a distributable release: exact third-party notices/source and signing are gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


def assert_plain(path: Path) -> None:
    for item in (path, *path.parents):
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("input links/reparse points forbidden")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise ValueError("only regular files/directories allowed")


def assert_plain_tree(root: Path) -> None:
    assert_plain(root)

    def failure(error):
        raise error

    count = 0
    for directory, directories, files in os.walk(root, followlinks=False, onerror=failure):
        for name in (*directories, *files):
            assert_plain(Path(directory) / name)
            count += 1
            if count > 32768:
                raise ValueError("input inventory limit")


def canonical_destination(path: Path) -> Path:
    """Resolve the existing prefix strictly, retaining only missing child names."""
    missing = []
    prefix = path
    while not os.path.lexists(prefix):
        if prefix.parent == prefix:
            raise ValueError("output requires an accessible existing parent")
        missing.append(prefix.name)
        prefix = prefix.parent
    return prefix.resolve(strict=True).joinpath(*reversed(missing))


def build(
    root: Path, archive: Path, expected: str, packages: Path, output: Path, release: str
) -> str:
    root, archive, packages, output = (
        Path(os.path.abspath(path)) for path in (root, archive, packages, output)
    )
    manifest_path = output.parent / f"{output.name}.manifest.json"
    if os.path.lexists(output) or os.path.lexists(manifest_path):
        raise ValueError("fresh output and manifest required")
    for parent in output.parents:
        if os.path.lexists(parent):
            assert_plain(parent)
    copied_roots = [
        packages,
        *(root / name for name in ("src", ".agents", "config", "schemas", "containers/asr")),
    ]
    assert_plain(archive)
    assert_plain_tree(packages)
    for name in ("src", ".agents", "config", "schemas", "containers/asr"):
        assert_plain_tree(root / name)
    for name in (
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
        "README.md",
        "native/worker_bootstrap.py",
    ):
        assert_plain(root / name)
    canonical_output = canonical_destination(output)
    for path in copied_roots:
        canonical_input = path.resolve(strict=True)
        if canonical_output.is_relative_to(canonical_input) or canonical_input.is_relative_to(
            canonical_output
        ):
            raise ValueError("output must not overlap copied inputs")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError("runtime archive hash mismatch")
    if output.exists() or not packages.is_dir():
        raise ValueError("fresh output and existing pinned packages are required")
    output.mkdir(parents=True)
    runtime = output / "runtime"
    runtime.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        if (
            len(bundle.infolist()) > 2048
            or sum(item.file_size for item in bundle.infolist()) > 256 * 1024 * 1024
        ):
            raise ValueError("runtime archive limit")
        for item in bundle.infolist():
            relative = PurePosixPath(item.filename)
            if (
                relative.is_absolute()
                or any(
                    part in (".", "..") or ":" in part or "\\" in part for part in relative.parts
                )
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise ValueError("unsafe runtime archive")
        bundle.extractall(runtime)
    if not (runtime / "python.exe").is_file() or not (runtime / "LICENSE.txt").is_file():
        raise ValueError("official Windows runtime and license required")
    # The builder receives a uv --locked export installed with --require-hashes
    # into this dedicated target. Dev/ASR extras are forbidden here.
    forbidden = ("pytest", "ruff", "faster_whisper", "ctranslate2", "av", "torch")
    if any((packages / name).exists() for name in forbidden):
        raise ValueError("development/ASR package in core payload")
    shutil.copytree(packages, output / "packages", symlinks=False)
    payload = output / "payload"
    payload.mkdir()
    for name in ("src", ".agents", "config", "schemas", "containers/asr"):
        shutil.copytree(
            root / name, payload / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    for name in (".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "README.md"):
        shutil.copy2(root / name, payload / name)
    shutil.copy2(root / "native/worker_bootstrap.py", payload / "bootstrap.py")
    entries = []
    for path in sorted(output.rglob("*")):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("payload links forbidden")
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    manifest = {
        "schema": "vep.sandbox-release.v1",
        "release_id": release,
        "target": "windows-x86_64",
        "python": "runtime/python.exe",
        "files": entries,
    }
    data = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    # Manifest is outside the input tree; native stage adds the verified copy.
    with manifest_path.open("xb") as stream:
        stream.write(data)
    identity = hashlib.sha256(data).hexdigest()
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "sha256": identity,
                "files": len(entries),
                "expanded_bytes": sum(e["size"] for e in entries),
                "development_only": True,
                "publisher_authenticated": False,
            }
        )
    )
    return identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("only the Windows development payload is implemented")
    build(
        args.root,
        args.runtime_archive,
        args.runtime_sha256,
        args.packages,
        args.output,
        args.release_id,
    )


if __name__ == "__main__":
    main()
