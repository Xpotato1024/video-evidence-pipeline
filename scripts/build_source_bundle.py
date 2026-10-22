"""Build a complete, commit-addressed source bundle locally. Never publish.

Includes only Git-tracked files, preserving .agents/config/schemas/uv.lock.
Refuses a dirty checkout so a bundle cannot silently omit the reviewed changes.
Third-party executables, Python environments and models are not bundled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

REQUIRED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "src/video_evidence_pipeline/cli.py",
    ".agents/skill-system.toml",
    ".agents/skills/refine-video-transcript/SKILL.md",
    "config/asr-profiles.yaml",
    "schemas/job-spec.schema.json",
    "containers/asr/Dockerfile",
    "tools/ffmpeg-toolchain.json",
)


def build(root: Path, output: Path) -> dict:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)

    if git("status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("Commit the reviewed changes before building a source bundle")
    revision = git("rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("A complete Git commit ID is required")
    output.mkdir(parents=True, exist_ok=True)
    prefix = f"video-evidence-pipeline-{revision}/"
    archive = output / f"video-evidence-pipeline-{revision}.zip"
    if archive.exists():
        raise FileExistsError("Output archive already exists; use a fresh output directory")
    git("archive", "--format=zip", f"--prefix={prefix}", f"--output={archive.resolve()}", revision)
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        if any(prefix + name not in names for name in REQUIRED_FILES):
            raise ValueError("Source bundle is missing required runtime resources")
        lock_sha256 = hashlib.sha256(bundle.read(prefix + "uv.lock")).hexdigest()
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    record = {
        "revision": revision,
        "archive": archive.name,
        "sha256": digest,
        "uv_lock_sha256": lock_sha256,
        "kind": "source-checkout",
        "third_party_binaries_included": False,
    }
    (output / "source-bundle.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    (output / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parents[1], args.output), sort_keys=True))


if __name__ == "__main__":
    main()
