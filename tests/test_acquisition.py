from __future__ import annotations

import json
import os
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from video_evidence_pipeline.acquisition.local_file import LocalFileAdapter
from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    initialize_job,
    job_status,
    validate_job,
)
from video_evidence_pipeline.domain.authorization import AuthorizationRecord
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import JobSpec, SourceKind, load_job_yaml
from video_evidence_pipeline.domain.manifests import SourceManifest
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.atomic import atomic_write_json, read_json_model
from video_evidence_pipeline.storage.jsonl import append_jsonl, read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace


def initialized_job(
    root: Path,
    source: Path | str,
    *,
    source_kind: SourceKind = SourceKind.LOCAL_FILE,
    requirements: list[str] | None = None,
) -> Path:
    workspace, _ = initialize_job(
        root,
        job_id="demo-report",
        source_kind=source_kind,
        source=str(source),
        task_type="report",
        language="ja",
        output_format="markdown",
        requirements=requirements,
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


def test_local_source_parent_junction_detection_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_parent = tmp_path / "source-parent"
    source_parent.mkdir()
    source = source_parent / "source.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    path_type = type(source)
    original = path_type.is_junction

    def fake_is_junction(path: Path) -> bool:
        return path == source_parent or original(path)

    monkeypatch.setattr(path_type, "is_junction", fake_is_junction)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)
    assert captured.value.code == "SOURCE_NOT_REGULAR_FILE"


def test_local_source_parent_symlink_is_rejected(tmp_path: Path) -> None:
    source_parent = tmp_path / "source-parent"
    source_parent.mkdir()
    source = source_parent / "source.bin"
    source.write_bytes(b"content")
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(source_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    job_path = initialized_job(tmp_path, linked_parent / source.name)
    authorize(job_path)

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


def test_concurrent_authorization_persists_only_the_successful_callers_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"stable content")
    job_path = initialized_job(tmp_path, source)
    entered_write = threading.Event()
    release_write = threading.Event()
    original_write = atomic_write_json
    caller_a_result: list[AuthorizationRecord | Exception] = []

    def blocked_write(target: Path, value: object, *, private: bool = False) -> None:
        if target.name == "authorization.json":
            entered_write.set()
            assert release_write.wait(timeout=5)
        original_write(target, value, private=private)

    def run_caller_a() -> None:
        try:
            caller_a_result.append(
                authorize_job(
                    job_path,
                    confirm=True,
                    basis="authorization-a",
                    allow_local_processing=True,
                    allow_external_api=False,
                    confirmer_identity="caller-a",
                )
            )
        except Exception as exc:  # pragma: no cover - assertion below reports the exception
            caller_a_result.append(exc)

    monkeypatch.setattr("video_evidence_pipeline.application.atomic_write_json", blocked_write)
    caller_a = threading.Thread(target=run_caller_a)
    caller_a.start()
    assert entered_write.wait(timeout=5)
    try:
        with pytest.raises(VepError) as caller_b_error:
            authorize_job(
                job_path,
                confirm=True,
                basis="authorization-b",
                allow_local_processing=True,
                allow_external_api=True,
                confirmer_identity="caller-b",
            )
        assert caller_b_error.value.code == "TRANSITION_LOG_BUSY"
    finally:
        release_write.set()
        caller_a.join(timeout=5)

    assert len(caller_a_result) == 1
    assert isinstance(caller_a_result[0], AuthorizationRecord)
    persisted = read_json_model(
        job_path / "authorization/authorization.json",
        AuthorizationRecord,
        code="AUTHORIZATION_INVALID",
    )
    assert persisted == caller_a_result[0]
    assert persisted.authorization_basis == "authorization-a"
    assert persisted.allow_local_processing is True
    assert persisted.allow_external_api is False
    assert persisted.confirmer_identity == "caller-a"
    authorization_text = (job_path / "authorization/authorization.json").read_text(encoding="utf-8")
    assert "authorization-b" not in authorization_text
    assert "caller-b" not in authorization_text
    transitions = read_jsonl(job_path / "manifests/transitions.jsonl", TransitionRecord)
    assert len(transitions) == 2
    assert transitions[-1].from_state.value == "CREATED"
    assert transitions[-1].to_state.value == "AUTHORIZED"
    status, exit_code = job_status(job_path)
    assert status["stored_state"] == "AUTHORIZED"
    assert exit_code == 0


def test_authorization_retry_recovers_record_without_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"stable content")
    job_path = initialized_job(tmp_path, source)

    def fail_transition_append(path: Path, record: object) -> None:
        raise RuntimeError("synthetic process interruption")

    monkeypatch.setattr("video_evidence_pipeline.application.append_jsonl", fail_transition_append)
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        authorize_job(
            job_path,
            confirm=True,
            basis="interrupted-authorization",
            allow_local_processing=True,
            allow_external_api=True,
            confirmer_identity="interrupted-caller",
        )

    interrupted = read_json_model(
        job_path / "authorization/authorization.json",
        AuthorizationRecord,
        code="AUTHORIZATION_INVALID",
    )
    assert interrupted.authorization_basis == "interrupted-authorization"
    transitions = read_jsonl(job_path / "manifests/transitions.jsonl", TransitionRecord)
    assert len(transitions) == 1
    assert transitions[-1].to_state.value == "CREATED"

    monkeypatch.setattr("video_evidence_pipeline.application.append_jsonl", append_jsonl)
    recovered = authorize_job(
        job_path,
        confirm=True,
        basis="recovered-authorization",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="recovery-caller",
    )

    persisted = read_json_model(
        job_path / "authorization/authorization.json",
        AuthorizationRecord,
        code="AUTHORIZATION_INVALID",
    )
    assert persisted == recovered
    assert persisted.authorization_basis == "recovered-authorization"
    assert persisted.allow_external_api is False
    assert persisted.confirmer_identity == "recovery-caller"
    transitions = read_jsonl(job_path / "manifests/transitions.jsonl", TransitionRecord)
    assert len(transitions) == 2
    assert transitions[-1].to_state.value == "AUTHORIZED"


def test_concurrent_acquisition_cannot_mix_manifest_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"stable content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    entered_adapter = threading.Event()
    release_adapter = threading.Event()
    original_acquire = LocalFileAdapter.acquire
    first_result: list[object] = []

    def blocked_acquire(adapter: LocalFileAdapter, job: JobSpec, workspace: JobWorkspace) -> object:
        entered_adapter.set()
        assert release_adapter.wait(timeout=5)
        return original_acquire(adapter, job, workspace)

    def run_first() -> None:
        try:
            first_result.append(acquire_job(job_path))
        except Exception as exc:  # pragma: no cover - assertion below reports the exception
            first_result.append(exc)

    monkeypatch.setattr(LocalFileAdapter, "acquire", blocked_acquire)
    first = threading.Thread(target=run_first)
    first.start()
    assert entered_adapter.wait(timeout=5)
    try:
        with pytest.raises(VepError) as captured:
            acquire_job(job_path)
        assert captured.value.code == "TRANSITION_LOG_BUSY"
    finally:
        release_adapter.set()
        first.join(timeout=5)

    assert len(first_result) == 1
    assert not isinstance(first_result[0], Exception)
    status, exit_code = job_status(job_path)
    assert status["stored_state"] == "ACQUIRED"
    assert status["artifact_integrity"]["status"] == "valid"
    assert exit_code == 0


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
    assert status["authorization"]["error_code"] == "AUTHORIZATION_STALE"
    assert exit_code == 3
    with pytest.raises(VepError) as validate_error:
        validate_job(job_path)
    assert validate_error.value.code == "AUTHORIZATION_STALE"


def test_copy_hash_mismatch_never_reaches_acquired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    expected_hash = sha256_file(source)
    original_hash = sha256_file
    hashes = iter([expected_hash, "b" * 64])
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
    assert not (job_path / f"source/raw/{expected_hash}/synthetic.bin").exists()

    monkeypatch.setattr("video_evidence_pipeline.acquisition.local_file.sha256_file", original_hash)
    retry = acquire_job(job_path)
    assert retry.source_manifest.artifact_record.artifact.content_sha256 == expected_hash


def test_preexisting_raw_target_is_never_deleted_on_integrity_failure(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    expected_hash = sha256_file(source)
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    raw = job_path / f"source/raw/{expected_hash}/synthetic.bin"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"corrupt")

    with pytest.raises(VepError) as captured:
        acquire_job(job_path)

    assert captured.value.code == "ARTIFACT_INTEGRITY_FAILED"
    assert raw.read_bytes() == b"corrupt"


def test_temporary_cleanup_failure_does_not_hide_original_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    expected_hash = sha256_file(source)
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    raw = job_path / f"source/raw/{expected_hash}/synthetic.bin"
    hashes = iter([expected_hash, "b" * 64])
    monkeypatch.setattr(
        "video_evidence_pipeline.acquisition.local_file.sha256_file",
        lambda _: next(hashes),
    )
    path_type = type(raw)
    original_unlink = path_type.unlink

    def fail_temporary_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.endswith(".tmp"):
            raise OSError("synthetic cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "unlink", fail_temporary_unlink)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)

    assert captured.value.code == "ARTIFACT_INTEGRITY_FAILED"
    assert not raw.exists()


def test_stage_finished_at_is_recorded_after_raw_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    base = datetime(2026, 8, 7, tzinfo=UTC)
    moments = iter([base, base + timedelta(seconds=1), base + timedelta(seconds=2)])
    verified = False
    original_verify = LocalFileAdapter._verify_raw

    def verify_then_mark(path: Path, artifact: object) -> None:
        nonlocal verified
        original_verify(path, artifact)  # type: ignore[arg-type]
        verified = True

    def controlled_now() -> datetime:
        value = next(moments)
        if value == base + timedelta(seconds=2):
            assert verified
        return value

    monkeypatch.setattr(LocalFileAdapter, "_verify_raw", staticmethod(verify_then_mark))
    monkeypatch.setattr("video_evidence_pipeline.acquisition.local_file._utc_now", controlled_now)

    result = acquire_job(job_path)

    assert result.stage_manifest.started_at == base
    assert result.source_manifest.created_at == base + timedelta(seconds=1)
    assert result.stage_manifest.finished_at == base + timedelta(seconds=2)


def test_backward_clock_does_not_create_an_invalid_stage_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    base = datetime(2026, 8, 7, tzinfo=UTC)
    moments = iter([base, base + timedelta(seconds=10), base + timedelta(seconds=2)])
    monkeypatch.setattr(
        "video_evidence_pipeline.acquisition.local_file._utc_now",
        lambda: next(moments),
    )

    result = acquire_job(job_path)

    assert result.source_manifest.created_at == base + timedelta(seconds=10)
    assert result.stage_manifest.finished_at == result.source_manifest.created_at
    status, exit_code = job_status(job_path)
    assert status["stored_state"] == "ACQUIRED"
    assert exit_code == 0


def test_future_partial_source_manifest_is_rejected_before_stage_persistence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    workspace = JobWorkspace.open(job_path)
    job = load_job_yaml(workspace.job_yaml)
    LocalFileAdapter().acquire(job, workspace)
    workspace.acquisition_manifest_json.unlink()
    payload = json.loads(workspace.source_manifest_json.read_text(encoding="utf-8"))
    payload["created_at"] = "2099-01-01T00:00:00Z"
    payload["artifact_record"]["created_at"] = "2099-01-01T00:00:00Z"
    workspace.source_manifest_json.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(VepError) as captured:
        LocalFileAdapter().acquire(job, workspace)

    assert captured.value.code == "SOURCE_MANIFEST_MISMATCH"
    assert not workspace.acquisition_manifest_json.exists()


def test_local_source_manifest_rejects_network_acquisition_evidence(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    acquire_job(job_path)
    workspace = JobWorkspace.open(job_path)
    payload = json.loads(workspace.source_manifest_json.read_text(encoding="utf-8"))
    payload["acquisition_evidence"] = {
        "adapter_kind": "direct_http",
        "transport": "https",
        "backend": "synthetic",
        "backend_version": "1",
        "obtained_at": payload["created_at"],
        "requested_host": "example.invalid",
        "final_host": "example.invalid",
        "redirect_count": 0,
        "http_status_category": "2xx",
        "response_content_type": "video/mp4",
        "declared_content_length": 7,
        "actual_acquired_size": 7,
        "credential_mode": "none",
        "warnings": [],
    }
    workspace.source_manifest_json.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VepError) as captured:
        LocalFileAdapter().acquire(load_job_yaml(workspace.job_yaml), workspace)
    assert captured.value.code == "SOURCE_MANIFEST_INVALID"


def test_validate_does_not_treat_denied_local_processing_as_spec_corruption(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize_job(
        job_path,
        confirm=True,
        basis="authorized synthetic fixture",
        allow_local_processing=False,
        allow_external_api=False,
        confirmer_identity="test-user",
    )
    assert validate_job(job_path)["valid"] is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not a Windows guarantee")
def test_posix_runtime_workspace_is_private(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(
        tmp_path,
        source,
        requirements=["first requirement", "second requirement"],
    )
    authorize(job_path)
    result = acquire_job(job_path)
    artifact = result.source_manifest.artifact_record.artifact
    directories = [
        tmp_path / ".local",
        tmp_path / ".local/jobs",
        job_path,
        job_path / "authorization",
        job_path / "source",
        job_path / "source/raw",
        (job_path / artifact.relative_storage_path).parent,
        job_path / "manifests",
        job_path / "manifests/stages",
        job_path / "logs",
    ]
    files = [
        job_path / "job.yaml",
        job_path / "authorization/authorization.json",
        job_path / "manifests/source-manifest.json",
        job_path / "manifests/stages/acquisition.json",
        job_path / "manifests/transitions.jsonl",
        job_path / artifact.relative_storage_path,
    ]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


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


def test_status_rejects_self_consistent_manifest_rebinding_to_job_yaml(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"content")
    job_path = initialized_job(tmp_path, source)
    authorize(job_path)
    acquire_job(job_path)
    job_yaml = job_path / "job.yaml"
    rebound_hash = sha256_file(job_yaml)
    rebound = {
        "artifact_id": f"source-{rebound_hash[:16]}",
        "artifact_type": "raw_source",
        "content_sha256": rebound_hash,
        "size_bytes": job_yaml.stat().st_size,
        "relative_storage_path": "job.yaml",
    }
    source_manifest_path = job_path / "manifests/source-manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_manifest["artifact_record"]["artifact"] = rebound
    source_manifest_path.write_text(json.dumps(source_manifest), encoding="utf-8")
    stage_manifest_path = job_path / "manifests/stages/acquisition.json"
    stage_manifest = json.loads(stage_manifest_path.read_text(encoding="utf-8"))
    stage_manifest["output_artifacts"] = [rebound]
    stage_manifest_path.write_text(json.dumps(stage_manifest), encoding="utf-8")

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
    normalized = TransitionRecord(
        sequence=4,
        job_id="demo-report",
        from_state=records[-1].to_state,
        to_state="NORMALIZED",
        trigger="tampered.unimplemented",
        occurred_at=records[-1].occurred_at,
        job_fingerprint=records[-1].job_fingerprint,
    )
    append = TransitionRecord(
        sequence=5,
        job_id="demo-report",
        from_state=normalized.to_state,
        to_state="TRANSCRIBED",
        trigger="tampered.unimplemented",
        occurred_at=records[-1].occurred_at,
        job_fingerprint=records[-1].job_fingerprint,
    )
    with transitions_path.open("ab") as handle:
        handle.write((normalized.model_dump_json() + "\n").encode())
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


def test_network_source_requires_dedicated_authorization(tmp_path: Path) -> None:
    locator = "https://example.invalid/video"
    job_path = initialized_job(tmp_path, locator, source_kind=SourceKind.DIRECT_HTTP)
    authorize(job_path)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path)
    assert captured.value.code == "NETWORK_ACQUISITION_NOT_AUTHORIZED"
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
