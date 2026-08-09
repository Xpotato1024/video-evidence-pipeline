"""Shared private staging and immutable publication for network adapters."""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from video_evidence_pipeline import __version__
from video_evidence_pipeline.acquisition.base import AcquisitionResult
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRecord,
    ArtifactRef,
    ProducerIdentity,
    ProducerKind,
)
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import JobSpec, SourceKind, job_fingerprint
from video_evidence_pipeline.domain.locators import locator_fingerprint
from video_evidence_pipeline.domain.manifests import (
    AcquisitionEvidence,
    CredentialMode,
    HttpStatusCategory,
    SourceManifest,
    StageManifest,
    StageStatus,
)
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.errors import integrity_error, io_error
from video_evidence_pipeline.storage.atomic import (
    atomic_copy_exclusive,
    atomic_write_json,
    read_json_model,
)
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    PRIVATE_FILE_MODE,
    enforce_private_descriptor,
    enforce_private_file,
    ensure_private_directory,
)


@dataclass(frozen=True, slots=True)
class AcquisitionEvidenceDraft:
    adapter_kind: SourceKind
    transport: str
    backend: str
    backend_version: str
    requested_host: str
    final_host: str | None
    redirect_count: int | None
    http_status_category: HttpStatusCategory
    response_content_type: str | None
    declared_content_length: int | None
    credential_mode: CredentialMode
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StagedSource:
    path: Path
    display_name: str
    evidence: AcquisitionEvidenceDraft
    cleanup_root: Path | None = None


def staging_directory(workspace: JobWorkspace) -> Path:
    directory = workspace.safe_path("source/staging")
    ensure_private_directory(directory)
    return directory


def new_staging_path(workspace: JobWorkspace, *, suffix: str = ".part") -> Path:
    return staging_directory(workspace) / f"{uuid.uuid4().hex}{suffix}"


def open_private_staging_file(path: Path):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
        return os.fdopen(descriptor, "wb")
    except OSError as exc:
        raise io_error(
            "SOURCE_STAGING_FAILED", "private source staging could not be created"
        ) from exc


def _verify_staged_file(path: Path, *, maximum_bytes: int) -> tuple[int, str]:
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise integrity_error("SOURCE_STAGING_INVALID", "staged source must not be a link")
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise integrity_error("SOURCE_STAGING_INVALID", "staged source must be a regular file")
        if metadata.st_size <= 0:
            raise integrity_error("SOURCE_EMPTY", "acquired source must not be empty")
        if metadata.st_size > maximum_bytes:
            raise integrity_error(
                "SOURCE_TOO_LARGE", "acquired source exceeds the configured limit"
            )
        enforce_private_file(path)
        return metadata.st_size, sha256_file(path)
    except OSError as exc:
        raise io_error("SOURCE_STAGING_FAILED", "staged source could not be verified") from exc


def _verify_raw(path: Path, artifact: ArtifactRef) -> None:
    size, content_sha256 = _verify_staged_file(path, maximum_bytes=artifact.size_bytes)
    if size != artifact.size_bytes or content_sha256 != artifact.content_sha256:
        raise integrity_error("ARTIFACT_INTEGRITY_FAILED", "raw artifact integrity is invalid")


def _validate_network_source_manifest(
    source_manifest: SourceManifest,
    job: JobSpec,
) -> ArtifactRef:
    artifact_record = source_manifest.artifact_record
    artifact = artifact_record.artifact
    evidence = source_manifest.acquisition_evidence
    expected_path = f"source/raw/{artifact.content_sha256}/{source_manifest.display_name}"
    if (
        job.source.kind is SourceKind.LOCAL_FILE
        or source_manifest.job_id != job.job_id
        or source_manifest.source_kind is not job.source.kind
        or source_manifest.display_name != sanitize_filename(source_manifest.display_name)
        or source_manifest.locator_fingerprint != locator_fingerprint(job.source.locator)
        or evidence is None
        or evidence.adapter_kind is not job.source.kind
        or evidence.obtained_at != source_manifest.created_at
        or evidence.actual_acquired_size != artifact.size_bytes
        or evidence.warnings != source_manifest.warnings
        or artifact.artifact_id != f"source-{artifact.content_sha256[:16]}"
        or artifact.artifact_type != "raw_source"
        or artifact.relative_storage_path != expected_path
        or artifact_record.input_artifacts
        or artifact_record.producer.kind is not ProducerKind.ADAPTER
        or artifact_record.producer.name != job.source.kind.value
        or artifact_record.configuration_sha256 != job_fingerprint(job)
        or artifact_record.redacted_error is not None
        or source_manifest.created_at != artifact_record.created_at
        or source_manifest.created_at > datetime.now(UTC)
    ):
        raise integrity_error(
            "SOURCE_MANIFEST_MISMATCH", "network source manifest does not match the job contract"
        )
    return artifact


def _validate_network_manifests(
    source_manifest: SourceManifest,
    stage_manifest: StageManifest,
    job: JobSpec,
) -> ArtifactRef:
    artifact = _validate_network_source_manifest(source_manifest, job)
    if (
        stage_manifest.job_id != job.job_id
        or stage_manifest.stage != "acquisition"
        or stage_manifest.status is not StageStatus.SUCCEEDED
        or stage_manifest.state_transition is not JobState.ACQUIRED
        or stage_manifest.input_artifacts
        or stage_manifest.output_artifacts != [artifact]
        or stage_manifest.producer != source_manifest.artifact_record.producer
        or stage_manifest.configuration_sha256 != job_fingerprint(job)
        or stage_manifest.redacted_error is not None
        or stage_manifest.started_at > source_manifest.created_at
        or stage_manifest.finished_at < source_manifest.created_at
        or stage_manifest.warnings != source_manifest.warnings
    ):
        raise integrity_error(
            "SOURCE_MANIFEST_MISMATCH", "network source bundle does not match the job contract"
        )
    return artifact


def recover_network_result(
    workspace: JobWorkspace,
    job: JobSpec,
) -> AcquisitionResult | None:
    """Reuse a complete bundle or finish the source-manifest crash window locally."""
    source_exists = workspace.source_manifest_json.exists()
    stage_exists = workspace.acquisition_manifest_json.exists()
    if not source_exists and not stage_exists:
        return None
    if not source_exists:
        raise integrity_error(
            "SOURCE_MANIFEST_INVALID",
            "network acquisition stage exists without its source manifest",
        )
    if stage_exists:
        return load_network_result(workspace, job)

    source_manifest = read_json_model(
        workspace.source_manifest_json, SourceManifest, code="SOURCE_MANIFEST_INVALID"
    )
    artifact = _validate_network_source_manifest(source_manifest, job)
    _verify_raw(workspace.safe_path(artifact.relative_storage_path), artifact)
    producer = source_manifest.artifact_record.producer
    stage_manifest = StageManifest(
        job_id=job.job_id,
        stage="acquisition",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.ACQUIRED,
        input_artifacts=[],
        output_artifacts=[artifact],
        producer=producer,
        configuration_sha256=job_fingerprint(job),
        started_at=source_manifest.created_at,
        finished_at=source_manifest.created_at,
        warnings=list(source_manifest.warnings),
    )
    _validate_network_manifests(source_manifest, stage_manifest, job)
    atomic_write_json(workspace.acquisition_manifest_json, stage_manifest, private=True)
    return AcquisitionResult(
        source_manifest=source_manifest,
        stage_manifest=stage_manifest,
        reused=True,
    )


def verify_network_bundle(workspace: JobWorkspace, job: JobSpec) -> ArtifactRef:
    source_manifest = read_json_model(
        workspace.source_manifest_json, SourceManifest, code="SOURCE_MANIFEST_INVALID"
    )
    stage_manifest = read_json_model(
        workspace.acquisition_manifest_json, StageManifest, code="STAGE_MANIFEST_INVALID"
    )
    artifact = _validate_network_manifests(source_manifest, stage_manifest, job)
    _verify_raw(workspace.safe_path(artifact.relative_storage_path), artifact)
    return artifact


def load_network_result(workspace: JobWorkspace, job: JobSpec) -> AcquisitionResult:
    verify_network_bundle(workspace, job)
    return AcquisitionResult(
        source_manifest=read_json_model(
            workspace.source_manifest_json, SourceManifest, code="SOURCE_MANIFEST_INVALID"
        ),
        stage_manifest=read_json_model(
            workspace.acquisition_manifest_json, StageManifest, code="STAGE_MANIFEST_INVALID"
        ),
        reused=True,
    )


def publish_staged_source(
    job: JobSpec,
    workspace: JobWorkspace,
    staged: StagedSource,
    *,
    maximum_bytes: int,
    started_at: datetime,
) -> AcquisitionResult:
    operation_failed = False
    try:
        size, content_sha256 = _verify_staged_file(staged.path, maximum_bytes=maximum_bytes)
        display_name = sanitize_filename(staged.display_name)
        relative_path = f"source/raw/{content_sha256}/{display_name}"
        artifact = ArtifactRef(
            artifact_id=f"source-{content_sha256[:16]}",
            artifact_type="raw_source",
            content_sha256=content_sha256,
            size_bytes=size,
            relative_storage_path=relative_path,
        )
        target = workspace.safe_path(relative_path)
        copy_result = atomic_copy_exclusive(
            staged.path,
            target,
            verifier=lambda candidate: _verify_raw(candidate, artifact),
        )
        _verify_raw(target, artifact)
        created_at = datetime.now(UTC)
        producer = ProducerIdentity(
            kind=ProducerKind.ADAPTER,
            name=job.source.kind.value,
            version=__version__,
        )
        evidence_data = asdict(staged.evidence)
        evidence_data["warnings"] = list(staged.evidence.warnings)
        evidence = AcquisitionEvidence(
            **evidence_data,
            obtained_at=created_at,
            actual_acquired_size=size,
        )
        source_manifest = SourceManifest(
            job_id=job.job_id,
            source_kind=job.source.kind,
            display_name=display_name,
            locator_fingerprint=locator_fingerprint(job.source.locator),
            artifact_record=ArtifactRecord(
                artifact=artifact,
                input_artifacts=[],
                producer=producer,
                configuration_sha256=job_fingerprint(job),
                created_at=created_at,
            ),
            acquisition_evidence=evidence,
            created_at=created_at,
            warnings=list(staged.evidence.warnings),
        )
        stage_manifest = StageManifest(
            job_id=job.job_id,
            stage="acquisition",
            status=StageStatus.SUCCEEDED,
            state_transition=JobState.ACQUIRED,
            input_artifacts=[],
            output_artifacts=[artifact],
            producer=producer,
            configuration_sha256=job_fingerprint(job),
            started_at=min(started_at, created_at),
            finished_at=max(datetime.now(UTC), created_at),
            warnings=list(staged.evidence.warnings),
        )
        _validate_network_manifests(source_manifest, stage_manifest, job)
        if workspace.source_manifest_json.exists() or workspace.acquisition_manifest_json.exists():
            existing = load_network_result(workspace, job)
            existing_artifact = existing.source_manifest.artifact_record.artifact
            if existing_artifact != artifact:
                raise integrity_error(
                    "SOURCE_CONTENT_CHANGED", "network source bundle already contains other content"
                )
            return existing
        atomic_write_json(workspace.source_manifest_json, source_manifest, private=True)
        atomic_write_json(workspace.acquisition_manifest_json, stage_manifest, private=True)
        return AcquisitionResult(
            source_manifest=source_manifest,
            stage_manifest=stage_manifest,
            reused=not copy_result.created,
        )
    except Exception:
        operation_failed = True
        raise
    finally:
        try:
            staged.path.unlink(missing_ok=True)
            if staged.cleanup_root is not None:
                staged.cleanup_root.rmdir()
        except OSError as exc:
            if not operation_failed:
                raise io_error(
                    "SOURCE_STAGING_CLEANUP_FAILED", "private source staging cleanup failed"
                ) from exc
