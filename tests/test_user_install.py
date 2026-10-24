"""Opt-in Windows user installer; every mutation stays in fresh test roots."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

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


@pytest.mark.parametrize(
    "point", ["user-init-before-root-publish", "user-init-marker", "user-init-layout"]
)
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


@pytest.mark.parametrize("name", ["CON", "NUL.txt", "trailing.", "trailing "])
def test_invalid_user_root_is_rejected_before_creating_any_sibling(tmp_path, name):
    before = list(tmp_path.iterdir())
    result, value = call(tmp_path / name, "init-user", confirmed=True)
    assert result.returncode and value["code"] == "PATH_UNSUPPORTED"
    assert list(tmp_path.iterdir()) == before


def test_junction_roots_and_state_are_rejected_without_target_changes(tmp_path):
    import _winapi

    target = tmp_path / "synthetic-target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_bytes(b"unrelated synthetic data\n")
    root = tmp_path / "junction-root"
    _winapi.CreateJunction(str(target), str(root))
    before = subprocess.check_output(["icacls", str(target)])
    result, value = call(root, "init-user", confirmed=True)
    assert result.returncode and value["code"] in {"LINK_FORBIDDEN", "FILE_TYPE_INVALID"}
    private = tmp_path / "private-root"
    assert call(private, "init-user", confirmed=True)[0].returncode == 0
    receipts = private / "state/receipts"
    receipts.rename(private / "state/receipts-retained")
    _winapi.CreateJunction(str(target), str(receipts))
    result, value = call(private, "status")
    assert result.returncode and value["code"] in {"LINK_FORBIDDEN", "FILE_TYPE_INVALID"}
    assert sentinel.read_bytes() == b"unrelated synthetic data\n"
    assert subprocess.check_output(["icacls", str(target)]) == before
    assert list(target.iterdir()) == [sentinel]


PAYLOAD = os.environ.get("VEP_USER_PAYLOAD")


def payload_fields(tmp_path, release):
    payload = Path(PAYLOAD).resolve()
    data = json.loads((payload.parent / "payload.manifest.json").read_bytes())
    data["release_id"] = release
    encoded = json.dumps(data).encode()
    path = tmp_path / ("manifest-" + hashlib.sha256(release.encode()).hexdigest()[:12] + ".json")
    path.write_bytes(encoded)
    return {
        "payload": str(payload),
        "manifest": str(path),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def install(root, fields, **extra):
    result, plan = call(root, "plan", **fields)
    assert result.returncode == 0, (result.stdout, result.stderr)
    result, value = call(
        root, "apply", **fields, plan_id=plan["value"]["plan_id"], accept_licenses=True, **extra
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return value


def installed_cli(root, *args, ok=True):
    executable = str(root / "app/bin/vep.exe")
    # Explicit application name avoids the Windows MAX_PATH module search limit
    # of CreateProcess(NULL, commandline), without changing system policy.
    if len(executable) >= 240 and not executable.startswith("\\\\?\\"):
        executable = "\\\\?\\" + executable
    result = subprocess.run(
        [executable, *args],
        executable=executable,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if ok:
        assert result.returncode == 0, (result.stdout, result.stderr)
    return result


@pytest.mark.skipif(not PAYLOAD, reason="user payload not supplied")
def test_user_real_payload_lifecycle_retained_launcher_and_jobs(tmp_path):
    root = tmp_path / "ユーザー導入"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    first, second = payload_fields(tmp_path, "user-one"), payload_fields(tmp_path, "user-two")
    before = (root / "state/active.json").read_bytes()
    result, plan = call(root, "plan", **first)
    assert result.returncode == 0 and plan["value"]["installation_mode"] == "user"
    assert (root / "state/active.json").read_bytes() == before
    denied, error = call(root, "apply", **first, plan_id=plan["value"]["plan_id"])
    assert denied.returncode and error["code"] == "LICENSE_ACCEPTANCE_REQUIRED"
    result, page = call(root, "licenses", **first)
    assert result.returncode == 0 and "third-party notices" in page["value"]["text"]
    assert page["value"]["next_offset"] > 0
    assert install(root, first)["value"]["active"]["generation"] == 1
    import msvcrt

    with (root / "state/writer.lock").open("r+b") as locked:
        msvcrt.locking(locked.fileno(), msvcrt.LK_NBLCK, 1)
        assert call(root, "status")[0].returncode == 0
        installed_cli(root, "--version")
        _, pending = call(root, "plan", **second)
        result, busy = call(
            root, "apply", **second, plan_id=pending["value"]["plan_id"], accept_licenses=True
        )
        assert result.returncode and busy["code"] == "OPERATION_IN_PROGRESS"
    assert "Usage:" in installed_cli(root, "--help").stdout
    assert (
        json.loads(installed_cli(root, "doctor", "--json").stdout)["installation_kind"]
        == "private-runtime"
    )
    source = tmp_path / "synthetic.txt"
    source.write_bytes(b"synthetic transcript\n")
    job = json.loads(
        installed_cli(
            root,
            "job",
            "init",
            "compatibility",
            "--source-kind",
            "local_file",
            "--source",
            str(source),
            "--primary-input",
            "transcript",
            "--task-type",
            "summary",
            "--language",
            "ja",
            "--output-format",
            "markdown",
            "--json",
        ).stdout
    )["workspace"]
    old_job = {
        p.relative_to(job).as_posix(): p.read_bytes() for p in Path(job).rglob("*") if p.is_file()
    }
    retained_engine = (root / "app/bin/vep-setup.exe").read_bytes()
    assert install(root, second)["value"]["active"]["generation"] == 2
    assert (root / "app/bin/vep-setup.exe").read_bytes() == retained_engine
    assert installed_cli(root, "job", "status", job, "--json", ok=False).returncode != 0
    _, rollback = call(root, "plan", **first)
    result, value = call(
        root, "rollback", release_id="user-one", plan_id=rollback["value"]["plan_id"]
    )
    assert result.returncode == 0 and value["value"]["active"]["generation"] == 3
    installed_cli(root, "job", "status", job, "--json")
    assert old_job == {
        p.relative_to(job).as_posix(): p.read_bytes() for p in Path(job).rglob("*") if p.is_file()
    }
    # A retained engine can restore a missing sibling; external bundle not used.
    (root / "app/bin/vep.exe").unlink()
    repaired = subprocess.run(
        [root / "app/bin/vep-setup.exe", "repair"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert repaired.returncode == 0, (repaired.stdout, repaired.stderr)
    installed_cli(root, "--version")
    assert (root / "app/versions/user-one").is_dir() and (root / "app/versions/user-two").is_dir()


@pytest.mark.skipif(not PAYLOAD, reason="user payload not supplied")
def test_user_rejects_reserved_release_and_tampered_sbom_without_state_mutation(tmp_path):
    root = tmp_path / "invalid"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    before = (root / "state/active.json").read_bytes()
    for release in ("CON", "nul.txt", "COM1", "trailing."):
        fields = payload_fields(tmp_path, release)
        result, error = call(root, "plan", **fields)
        assert result.returncode and error["code"] == "MANIFEST_INVALID"
    fields = payload_fields(tmp_path, "sbom-mismatch")
    manifest = Path(fields["manifest"])
    data = json.loads(manifest.read_bytes())
    next(e for e in data["files"] if e["path"] == "payload/bootstrap.py")["sha256"] = "0" * 64
    encoded = json.dumps(data).encode()
    manifest.write_bytes(encoded)
    fields["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    result, error = call(root, "plan", **fields)
    assert result.returncode and error["code"] == "SBOM_INVENTORY_MISMATCH"
    assert (root / "state/active.json").read_bytes() == before


@pytest.mark.skipif(not PAYLOAD, reason="user payload not supplied")
@pytest.mark.parametrize("point", ["bootstrap-receipt", "bootstrap-staged", "bootstrap-published"])
def test_entrypoint_interruption_recovers_from_retained_version(tmp_path, point):
    root = tmp_path / "interrupted"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    fields = payload_fields(tmp_path, "bootstrap-recovery")
    _, plan = call(root, "plan", **fields)
    result, _ = call(
        root,
        "apply",
        **fields,
        plan_id=plan["value"]["plan_id"],
        accept_licenses=True,
        environment=dict(os.environ, VEP_SANDBOX_INTERRUPT=point),
    )
    assert result.returncode == 86
    # Repair uses only the internal bootstrap receipt and retained verified version.
    result, repaired = call(root, "repair")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert repaired["value"]["active"]["generation"] == 0
    install(root, fields)
    installed_cli(root, "--version")


@pytest.mark.skipif(not PAYLOAD, reason="user payload not supplied")
def test_user_cancel_before_commit_preserves_active_and_data(tmp_path):
    root = tmp_path / "cancelled"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    sentinel = root / "data/synthetic.txt"
    sentinel.write_bytes(b"retained\n")
    fields = payload_fields(tmp_path, "user-cancel")
    _, plan = call(root, "plan", **fields)
    process = subprocess.Popen(
        [ENGINE, "--user-root", str(root), "--protocol=jsonl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        process.stdin.write(
            json.dumps(
                {
                    "schema": "vep.setup.v1",
                    "id": "cancel-test",
                    "op": "apply",
                    "events": True,
                    "accept_licenses": True,
                    "plan_id": plan["value"]["plan_id"],
                    **fields,
                }
            )
            + "\n"
        )
        process.stdin.flush()
        first = json.loads(process.stdout.readline())
        assert first["event"] == "progress"
        with pytest.raises(OSError):
            (root / "app/staging").rename(tmp_path / "must-not-move")
        process.stdin.write('{"schema":"vep.setup.v1","id":"cancel-test","op":"cancel"}\n')
        process.stdin.flush()
        output, error = process.communicate(timeout=180)
        last = json.loads(output.splitlines()[-1])
        assert last["status"] == "cancelled", (output, error)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert call(root, "status")[1]["value"]["active"]["generation"] == 0
    assert sentinel.read_bytes() == b"retained\n"


@pytest.mark.skipif(not PAYLOAD, reason="user payload not supplied")
def test_long_japanese_user_root_runs_private_runtime(tmp_path):
    root = tmp_path.joinpath(*[("日本語の長い試験フォルダー" * 2) + str(i) for i in range(8)])
    os.makedirs("\\\\?\\" + str(root.parent))
    assert len(str(root)) > 260
    result, _ = call(root, "init-user", confirmed=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    fields = payload_fields(tmp_path, "long-root")
    install(root, fields)
    assert (
        json.loads(installed_cli(root, "doctor", "--json").stdout)["installation_kind"]
        == "private-runtime"
    )
