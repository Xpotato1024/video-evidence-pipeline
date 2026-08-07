from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.atomic import atomic_write_bytes
from video_evidence_pipeline.storage.jsonl import append_jsonl, read_jsonl
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
