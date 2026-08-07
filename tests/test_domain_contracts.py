from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import JobSpec, job_fingerprint
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord, validate_transition_records
from video_evidence_pipeline.errors import VepError


def job_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": "1.0",
        "job_id": "demo-report",
        "created_at": "2026-08-07T00:00:00Z",
        "source": {"kind": "local_file", "locator": "C:/synthetic/source.bin"},
        "assignment": {"task_type": "report", "language": "ja", "requirements": []},
        "output": {"format": "markdown"},
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "job_id",
    ["a", "demo-report", "a" * 63],
)
def test_valid_job_ids(job_id: str) -> None:
    assert JobSpec.model_validate(job_data(job_id=job_id)).job_id == job_id


@pytest.mark.parametrize(
    "job_id",
    ["", "Demo", "-demo", "demo_report", "../demo", "/absolute", "a" * 64],
)
def test_invalid_job_ids(job_id: str) -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate(job_data(job_id=job_id))


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate({**job_data(), "unexpected": True})


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate(job_data(created_at="2026-08-07T00:00:00"))


def test_machine_datetime_is_normalized_to_utc() -> None:
    job = JobSpec.model_validate(job_data(created_at="2026-08-07T09:00:00+09:00"))
    assert job.created_at.utcoffset().total_seconds() == 0
    assert job.model_dump_json().find("2026-08-07T00:00:00Z") >= 0


def test_yaml_formatting_and_key_order_do_not_change_fingerprint() -> None:
    compact = """
schema_version: '1.0'
job_id: demo-report
created_at: 2026-08-07T00:00:00Z
source: {kind: local_file, locator: C:/synthetic/source.bin}
assignment: {task_type: report, language: ja, requirements: []}
output: {format: markdown}
"""
    reordered = """
output:
  format: markdown
assignment:
  requirements: []
  language: ja
  task_type: report
source:
  locator: C:/synthetic/source.bin
  kind: local_file
created_at: 2026-08-07T00:00:00+00:00
job_id: demo-report
schema_version: '1.0'
"""
    first = JobSpec.model_validate(yaml.safe_load(compact))
    second = JobSpec.model_validate(yaml.safe_load(reordered))
    assert job_fingerprint(first) == job_fingerprint(second)


def test_semantic_change_changes_fingerprint() -> None:
    first = JobSpec.model_validate(job_data())
    second = JobSpec.model_validate(
        job_data(output={"format": "html"}),
    )
    assert job_fingerprint(first) != job_fingerprint(second)


def test_streaming_file_hash(tmp_path: Path) -> None:
    content = (b"0123456789abcdef" * 200_000) + b"tail"
    source = tmp_path / "large.bin"
    source.write_bytes(content)
    assert sha256_file(source, chunk_size=1024) == sha256_bytes(content)


@pytest.mark.parametrize("path", ["../raw.bin", "/raw.bin", "C:\\raw.bin", "source/../raw"])
def test_artifact_path_traversal_is_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id="source-1",
            artifact_type="raw_source",
            content_sha256="a" * 64,
            size_bytes=1,
            relative_storage_path=path,
        )


def test_filename_sanitization() -> None:
    assert sanitize_filename("../unsafe video?.mp4") == "unsafe_video_.mp4"
    assert sanitize_filename("..") == "source"
    assert len(sanitize_filename("x" * 200 + ".mp4")) <= 128
    assert sanitize_filename("CON.mp4") == "source_CON.mp4"


def test_sensitive_url_query_is_rejected() -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate(
            job_data(
                source={
                    "kind": "direct_http",
                    "locator": "https://example.invalid/video?" + "token=" + "sensitive-value",
                }
            )
        )


def transition(
    sequence: int,
    from_state: JobState | None,
    to_state: JobState,
) -> TransitionRecord:
    return TransitionRecord(
        sequence=sequence,
        job_id="demo",
        from_state=from_state,
        to_state=to_state,
        trigger="test",
        occurred_at=datetime.now(UTC),
        job_fingerprint="a" * 64,
    )


def test_valid_state_transitions() -> None:
    records = [
        transition(1, None, JobState.CREATED),
        transition(2, JobState.CREATED, JobState.AUTHORIZED),
        transition(3, JobState.AUTHORIZED, JobState.ACQUIRED),
    ]
    assert validate_transition_records(records) is JobState.ACQUIRED


def test_invalid_state_transition_is_rejected() -> None:
    records = [
        transition(1, None, JobState.CREATED),
        transition(2, JobState.CREATED, JobState.ACQUIRED),
    ]
    with pytest.raises(VepError, match="not allowed"):
        validate_transition_records(records)
