from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile

import pytest

from scripts.build_source_bundle import REQUIRED_FILES, build


def test_source_bundle_contains_hidden_runtime_contract_and_commit_identity(tmp_path):
    root = tmp_path / "source"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)

    git("init")
    for name in REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic source fixture\n", encoding="utf-8")
    (root / ".gitignore").write_text(".local/\n", encoding="utf-8")
    git("add", ".")
    git(
        "-c",
        "user.name=VEP synthetic test",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "synthetic fixture",
    )
    # Ignored local data must not enter the source archive.
    (root / ".local").mkdir()
    (root / ".local/private.txt").write_text("do not distribute", encoding="utf-8")
    output = tmp_path / "bundle"
    record = build(root, output)
    assert record["revision"] == git("rev-parse", "HEAD").decode().strip()
    archive = output / record["archive"]
    assert record["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert json.loads((output / "source-bundle.json").read_text()) == record
    with zipfile.ZipFile(archive) as bundle:
        assert any(name.endswith("/.agents/skill-system.toml") for name in bundle.namelist())
        assert not any(".local/" in name or "/.git/" in name for name in bundle.namelist())
    second = build(root, tmp_path / "second")
    assert second == record
    (root / "unreviewed.txt").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(ValueError, match="Commit the reviewed changes"):
        build(root, tmp_path / "dirty")
