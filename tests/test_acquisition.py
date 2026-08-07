from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    initialize_job,
    job_status,
)
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.manifests import SourceManifest
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.atomic import read_json_model
from video_evidence_pipeline.storage.jsonl import read_jsonl


def initialized_job(
    root: Path,
    source: Path | str,
    *,
    source_kind: SourceKind = SourceKind.LOCAL_FILE,
) -> Path:
    workspace, _ = initialize_job(
        root,
        job_id="demo-report",
        source_kind=source_kind,
        source=str(source),
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    return workspace.root


def authorize(path: Path) -> None:
    authorize_job(
        path,
        confirm=True,
        basis="authorized synthetic fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="test-user",
    )


def test_valid_local_file_acquisition(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic source bytes")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    result = acquire_job(job_path)

    artifact = result.source_manifest.artifact_record.artifact
    raw = job_path / Path(artifact.relative_storage_path)
    assert raw.read_bytes() == source.read_bytes()
    assert artifact.content_sha256 == sha256_file(raw)
    assert result.stage_manifest.output_artifacts == [artifact]
    assert len(read_jsonl(job_path / "manifests/transitions.jsonl", TransitionRecord)) == 3


@pytest.mark.parametrize("kind", ["missing", "empty", "directory"])
def test_invalid_local_sources_are_rejected(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source.bin"
    if kind == "empty":
        source.touch()
    elif kind == "directory":
        source.mkdir()
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)
    expected = {
        "missing": "SOURCE_NOT_FOUND",
        "empty": "SOURCE_EMPTY",
        "directory": "SOURCE_NOT_REGULAR_FILE",
    }
    assert captured.value.code == expected[kind]


def test_local_source_junction_detection_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    path_type = type(source)
    original = path_type.is_junction

    def fake_is_junction(path: Path) -> bool:
        return path == source or original(path)

    monkeypatch.setattr(path_type, "is_junction", fake_is_junction)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)
    assert captured.value.code == "SOURCE_NOT_REGULAR_FILE"


def test_idempotent_rerun_reuses_artifact_and_manifests(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"stable content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    first = acquire_job(job_path)
    transition_bytes = (job_path / "manifests/transitions.jsonl").read_bytes()
    source_manifest_bytes = (job_path / "manifests/source-manifest.json").read_bytes()
    stage_manifest_bytes = (job_path / "manifests/stages/acquisition.json").read_bytes()

    second = acquire_job(job_path)

    assert not first.reused
    assert second.reused
    assert (job_path / "manifests/transitions.jsonl").read_bytes() == transition_bytes
    assert (job_path / "manifests/source-manifest.json").read_bytes() == source_manifest_bytes
    assert (job_path / "manifests/stages/acquisition.json").read_bytes() == stage_manifest_bytes


def test_same_locator_content_change_is_rejected_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    original = b"original source"
    source.write_bytes(original)
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    first = acquire_job(job_path)
    raw = job_path / first.source_manifest.artifact_record.artifact.relative_storage_path

    source.write_bytes(b"changed source")
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)

    assert captured.value.code == "SOURCE_CONTENT_CHANGED"
    assert raw.read_bytes() == original
    assert list((job_path / "source/raw").rglob("*.bin")) == [raw]


def test_authorization_stale_after_semantic_job_change(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"stable")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    job_yaml = job_path / "job.yaml"
    data = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
    data["output"]["format"] = "html"
    job_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(VepError) as captured:
        acquire_job(job_path)
    assert captured.value.code == "AUTHORIZATION_STALE"
    status, exit_code = job_status(job_path)
    assert status["authorization"]["stale"] is True
    assert exit_code == 3


def test_copy_hash_mismatch_never_reaches_acquired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    hashes = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(
        "video_evidence_pipeline.acquisition.local_file.sha256_file",
        lambda _: next(hashes),
    )
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)
    assert captured.value.code == "ARTIFACT_INTEGRITY_FAILED"
    transitions = read_jsonl(job_path / "manifests/transitions.jsonl", TransitionRecord)
    assert transitions[-1].to_state.value == "AUTHORIZED"
    assert not (job_path / "manifests/source-manifest.json").exists()


def test_manifest_does_not_leak_absolute_source_or_query_secret(tmp_path: Path) -> None:
    source = tmp_path / "token-do-not-copy" / "video.bin"
    source.parent.mkdir()
    source.write_bytes(b"synthetic")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    acquire_job(job_path)
    manifest_text = (job_path / "manifests/source-manifest.json").read_text(encoding="utf-8")
    stage_text = (job_path / "manifests/stages/acquisition.json").read_text(encoding="utf-8")
    assert str(source.absolute()) not in manifest_text
    assert str(source.absolute()) not in stage_text
    assert "token-do-not-copy" not in manifest_text
    assert "token-do-not-copy" not in stage_text


def test_status_reports_tampered_artifact(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    result = acquire_job(job_path)
    artifact = result.source_manifest.artifact_record.artifact
    (job_path / artifact.relative_storage_path).write_bytes(b"tampered")
    status, exit_code = job_status(job_path)
    assert status["artifact_integrity"]["status"] == "invalid"
    assert exit_code == 5


def test_status_reports_missing_acquisition_stage_manifest(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    acquire_job(job_path)
    (job_path / "manifests/stages/acquisition.json").unlink()

    status, exit_code = job_status(job_path)

    assert status["artifact_integrity"]["error_code"] == "STAGE_MANIFEST_INVALID"
    assert exit_code == 5


def test_status_reports_source_manifest_job_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    acquire_job(job_path)
    manifest_path = job_path / "manifests/source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["job_id"] = "another-job"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    status, exit_code = job_status(job_path)

    assert status["artifact_integrity"]["error_code"] == "SOURCE_MANIFEST_MISMATCH"
    assert exit_code == 5


def test_unimplemented_transition_is_rejected_before_integrity_status(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    result = acquire_job(job_path)
    transitions_path = job_path / "manifests/transitions.jsonl"
    records = read_jsonl(transitions_path, TransitionRecord)
    append = TransitionRecord(
        sequence=4,
        job_id="demo-report",
        from_state=records[-1].to_state,
        to_state="NORMALIZED",
        trigger="tampered.unimplemented",
        occurred_at=records[-1].occurred_at,
        job_fingerprint=records[-1].job_fingerprint,
    )
    with transitions_path.open("ab") as handle:
        handle.write((append.model_dump_json() + "\n").encode())
    artifact = result.source_manifest.artifact_record.artifact
    (job_path / artifact.relative_storage_path).write_bytes(b"tampered")

    with pytest.raises(VepError) as captured:
        job_status(job_path)
    assert captured.value.code == "TRANSITION_NOT_IMPLEMENTED"


@pytest.mark.parametrize("field", ["job_id", "job_fingerprint"])
def test_initial_transition_binding_is_checked_before_authorization(
    tmp_path: Path, field: str
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    transitions_path = job_path / "manifests/transitions.jsonl"
    record = json.loads(transitions_path.read_text(encoding="utf-8"))
    record[field] = "another-job" if field == "job_id" else "b" * 64
    transitions_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(VepError) as captured:
        authorize(job_path)

    expected = {
        "job_id": "TRANSITION_JOB_MISMATCH",
        "job_fingerprint": "TRANSITION_FINGERPRINT_MISMATCH",
    }
    assert captured.value.code == expected[field]
    assert not (job_path / "authorization/authorization.json").exists()


def test_nonimplemented_source_kind_fails_explicitly(tmp_path: Path) -> None:
    locator = "https://example.invalid/video"
    job_path = initialized_job(tmp_path, locator, source_kind=SourceKind.DIRECT_HTTP)
    authorize(job_path)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)
    assert captured.value.code == "SOURCE_KIND_NOT_IMPLEMENTED"
    assert "do-not-log" not in captured.value.safe_message


def test_status_reports_missing_authorization_record(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    (job_path / "authorization/authorization.json").unlink()
    status, exit_code = job_status(job_path)
    assert status["authorization"]["error_code"] == "AUTHORIZATION_MISSING"
    assert exit_code == 3


def test_source_manifest_round_trip_is_strict(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    acquire_job(job_path)
    manifest_path = job_path / "manifests/source-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["unexpected"] = True
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(VepError) as captured:
        read_json_model(manifest_path, SourceManifest, code="SOURCE_MANIFEST_INVALID")
    assert captured.value.code == "SOURCE_MANIFEST_INVALID"


def test_raw_artifact_hash_matches_known_content(tmp_path: Path) -> None:
    content = b"known synthetic content"
    source = tmp_path / "synthetic.bin"
    source.write_bytes(content)
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    result = acquire_job(job_path)
    assert result.source_manifest.artifact_record.artifact.content_sha256 == sha256_bytes(content)
