"""Opt-in Windows user installer; every mutation stays in fresh test roots."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

ENGINE = os.environ.get("VEP_USER_SETUP_BIN")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not ENGINE, reason="Windows user installer not supplied"
)


def call(root, op, *, environment=None, **fields):
    request = {"schema": "vep.setup.v1", "id": "user-test", "op": op, **fields}
    result = subprocess.run(
        [ENGINE, "--user-root", str(root), "--protocol=jsonl"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=environment,
    )
    return result, json.loads(result.stdout) if result.stdout else {}


def test_user_root_confirmation_identity_and_repeated_init_preserve_data(tmp_path):
    root = tmp_path / "新しい導入先"
    result, value = call(root, "init-user")
    assert value["code"] == "CONFIRMATION_REQUIRED" and not root.exists()
    result, value = call(root, "init-user", confirmed=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert value["value"]["installation_mode"] == "user"
    identity = value["value"]["installation_id"]
    sentinel = root / "data/keep-synthetic.txt"
    sentinel.write_bytes(b"synthetic user data\n")
    result, value = call(root, "init-user", confirmed=True)
    assert result.returncode == 0 and value["value"]["installation_id"] == identity
    assert sentinel.read_bytes() == b"synthetic user data\n"
    marker = json.loads((root / ".vep-user-install.json").read_bytes())
    assert marker["phase"] == "ready"


@pytest.mark.parametrize("point", ["user-init-marker", "user-init-layout"])
def test_user_initialization_resume_after_process_interrupt(tmp_path, point):
    root = tmp_path / "install"
    result, _ = call(
        root, "init-user", confirmed=True, environment=dict(os.environ, VEP_SANDBOX_INTERRUPT=point)
    )
    assert result.returncode == 86
    result, value = call(root, "init-user", confirmed=True)
    assert result.returncode == 0 and value["value"]["active"]["generation"] == 0


def test_existing_nonprivate_or_foreign_root_is_not_adopted_or_repermissioned(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    sentinel = root / "another-install.txt"
    sentinel.write_bytes(b"preserve foreign installation\n")
    before = subprocess.check_output(["icacls", str(root)])
    result, value = call(root, "init-user", confirmed=True)
    assert result.returncode and value["code"] in {
        "ROOT_PRIVACY_UNVERIFIED",
        "FOREIGN_INSTALLATION_ROOT",
    }
    assert sentinel.read_bytes() == b"preserve foreign installation\n"
    assert subprocess.check_output(["icacls", str(root)]) == before
    assert not (root / ".vep-user-install.json").exists()


def test_user_root_marker_cannot_be_reused_at_another_path(tmp_path):
    root = tmp_path / "one"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    moved = tmp_path / "two"
    root.rename(moved)
    result, value = call(moved, "status")
    assert result.returncode and value["code"] == "ROOT_IDENTITY_MISMATCH"


def test_linked_state_is_rejected_without_writing_the_other_name(tmp_path):
    root = tmp_path / "install"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    active = root / "state/active.json"
    alias = tmp_path / "synthetic-alias.json"
    os.link(active, alias)
    before = alias.read_bytes()
    result, value = call(root, "status")
    assert result.returncode and value["code"] == "HARDLINK_FORBIDDEN"
    assert alias.read_bytes() == before
