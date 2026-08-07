from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.atomic import atomic_copy_exclusive, atomic_write_bytes
from video_evidence_pipeline.storage.jsonl import (
    append_jsonl,
    exclusive_append_lock,
    read_jsonl,
)
from video_evidence_pipeline.storage.layout import JobWorkspace


def record(sequence: int = 1) -> TransitionRecord:
    return TransitionRecord(
        sequence=sequence,
        job_id="demo",
        from_state=None,
        to_state=JobState.CREATED,
        trigger="test",
        occurred_at=datetime.now(UTC),
        job_fingerprint="a" * 64,
    )


def test_atomic_write_replaces_complete_content(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "value.json"
    atomic_write_bytes(target, b'{"first":true}\n')
    atomic_write_bytes(target, b'{"second":true}\n')
    assert json.loads(target.read_text(encoding="utf-8")) == {"second": True}
    assert not list(target.parent.glob("*.tmp"))


def test_append_only_jsonl_preserves_records(tmp_path: Path) -> None:
    target = tmp_path / "transitions.jsonl"
    append_jsonl(target, record(1))
    first = target.read_bytes()
    append_jsonl(target, record(2))
    assert target.read_bytes().startswith(first)
    assert len(read_jsonl(target, TransitionRecord)) == 2


@pytest.mark.parametrize(
    "payload,code", [(b'{"bad":}\n', "JSONL_MALFORMED"), (b"{}", "JSONL_INCOMPLETE")]
)
def test_malformed_or_incomplete_jsonl_fails(tmp_path: Path, payload: bytes, code: str) -> None:
    target = tmp_path / "transitions.jsonl"
    target.write_bytes(payload)
    with pytest.raises(VepError) as captured:
        read_jsonl(target, TransitionRecord)
    assert captured.value.code == code


def test_workspace_escape_is_rejected(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(tmp_path, "demo")
    with pytest.raises(ValueError, match="traversal"):
        workspace.safe_path("../outside")


def test_workspace_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / ".local" / "jobs" / "linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(VepError) as captured:
        JobWorkspace.open(link)
    assert captured.value.code == "WORKSPACE_SYMLINK"


def test_workspace_junction_detection_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".local").mkdir()
    path_type = type(tmp_path)
    original = path_type.is_junction

    def fake_is_junction(path: Path) -> bool:
        return path.name == ".local" or original(path)

    monkeypatch.setattr(path_type, "is_junction", fake_is_junction)
    with pytest.raises(VepError) as captured:
        JobWorkspace.create(tmp_path, "demo")
    assert captured.value.code == "WORKSPACE_SYMLINK"


def test_private_permission_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "video_evidence_pipeline.storage.permissions.POSIX_MODE_ENFORCED",
        True,
    )

    def fail_chmod(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic permission failure")

    monkeypatch.setattr("video_evidence_pipeline.storage.permissions.os.chmod", fail_chmod)
    with pytest.raises(VepError) as captured:
        JobWorkspace.create(tmp_path, "demo")
    assert captured.value.code == "PRIVATE_PERMISSION_FAILED"


def test_exclusive_copy_preserves_a_concurrent_writers_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"expected")
    target = tmp_path / "raw/hash/target.bin"
    target.parent.mkdir(parents=True)

    def publish_competing_target(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"competitor")
        raise FileExistsError

    monkeypatch.setattr("video_evidence_pipeline.storage.atomic.os.link", publish_competing_target)
    result = atomic_copy_exclusive(source, target, verifier=lambda path: None)

    assert result.created is False
    assert target.read_bytes() == b"competitor"


def test_process_lock_is_released_when_holder_is_killed(tmp_path: Path) -> None:
    transitions = tmp_path / "transitions.jsonl"
    marker = tmp_path / "lock-acquired"
    child_code = """
import sys
import time
from pathlib import Path
from video_evidence_pipeline.storage.jsonl import exclusive_append_lock

with exclusive_append_lock(Path(sys.argv[1])):
    Path(sys.argv[2]).touch()
    time.sleep(60)
"""
    process = subprocess.Popen([sys.executable, "-c", child_code, str(transitions), str(marker)])
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "child did not acquire the process lock"
        process.kill()
        process.wait(timeout=10)

        with exclusive_append_lock(transitions):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_process_lock_rejects_a_symlink_target(tmp_path: Path) -> None:
    transitions = tmp_path / "transitions.jsonl"
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not modify")
    lock = tmp_path / ".transitions.jsonl.lock"
    try:
        lock.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(VepError) as captured, exclusive_append_lock(transitions):
        pass

    assert captured.value.code == "TRANSITION_LOCK_FAILED"
    assert outside.read_bytes() == b"do not modify"


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not a Windows guarantee")
def test_private_temporary_and_lock_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "private.json"
    observed_temporary_modes: list[int] = []
    original_replace = os.replace

    def inspect_replace(source: Path, destination: Path) -> None:
        observed_temporary_modes.append(stat.S_IMODE(source.stat().st_mode))
        original_replace(source, destination)

    monkeypatch.setattr("video_evidence_pipeline.storage.atomic.os.replace", inspect_replace)
    atomic_write_bytes(target, b"{}\n", private=True)
    assert observed_temporary_modes == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    copy_source = tmp_path / "copy-source.bin"
    copy_source.write_bytes(b"content")
    (tmp_path / "raw").mkdir()
    copy_target = tmp_path / "raw/hash/copy-target.bin"
    observed_copy_temporary_modes: list[int] = []
    original_link = os.link

    def inspect_link(source: Path, destination: Path) -> None:
        observed_copy_temporary_modes.append(stat.S_IMODE(source.stat().st_mode))
        original_link(source, destination)

    monkeypatch.setattr("video_evidence_pipeline.storage.atomic.os.link", inspect_link)
    copy_result = atomic_copy_exclusive(
        copy_source,
        copy_target,
        verifier=lambda path: None,
    )
    assert copy_result.created is True
    assert observed_copy_temporary_modes == [0o600]
    assert stat.S_IMODE(copy_target.stat().st_mode) == 0o600

    transitions = tmp_path / "transitions.jsonl"
    with exclusive_append_lock(transitions):
        lock = tmp_path / ".transitions.jsonl.lock"
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    append_jsonl(transitions, record())
    assert stat.S_IMODE(transitions.stat().st_mode) == 0o600
