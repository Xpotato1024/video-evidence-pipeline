"""Immutable local_file acquisition."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from video_evidence_pipeline import __version__
from video_evidence_pipeline.acquisition.base import (
    AcquisitionAdapter,
    AcquisitionResult,
    ProbeResult,
)
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRecord,
    ArtifactRef,
    ProducerIdentity,
    ProducerKind,
)
from video_evidence_pipeline.domain.hashing import local_locator_fingerprint, sha256_file
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import JobSpec, SourceKind, job_fingerprint
from video_evidence_pipeline.domain.manifests import SourceManifest, StageManifest, StageStatus
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.errors import integrity_error, io_error
from video_evidence_pipeline.storage.atomic import (
    atomic_copy_exclusive,
    atomic_write_json,
    read_json_model,
)
from video_evidence_pipeline.storage.layout import JobWorkspace


def _safe_display_name(path: Path) -> str:
    visible = path.name.split("?", 1)[0].split("#", 1)[0]
    return sanitize_filename(visible)


class LocalFileAdapter(AcquisitionAdapter):
    def _source_path(self, job: JobSpec) -> Path:
        return Path(job.source.locator)

    def probe(self, job: JobSpec) -> ProbeResult:
        if job.source.kind is not SourceKind.LOCAL_FILE:
            raise integrity_error("ADAPTER_KIND_MISMATCH", "local adapter received another kind")
        source = self._source_path(job)
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise io_error("SOURCE_NOT_REGULAR_FILE", "local source must be a regular file")
        try:
            if not source.exists():
                raise io_error("SOURCE_NOT_FOUND", "local source does not exist")
            if not source.is_file():
                raise io_error("SOURCE_NOT_REGULAR_FILE", "local source must be a regular file")
            size = source.stat().st_size
        except OSError as exc:
            raise io_error("SOURCE_PROBE_FAILED", "local source could not be inspected") from exc
        if size <= 0:
            raise integrity_error("SOURCE_EMPTY", "local source must not be empty")
        try:
            content_sha256 = sha256_file(source)
        except OSError as exc:
            raise io_error("SOURCE_HASH_FAILED", "local source could not be hashed") from exc
        return ProbeResult(
            display_name=_safe_display_name(source),
            locator_fingerprint=local_locator_fingerprint(source),
            content_sha256=content_sha256,
            size_bytes=size,
        )

    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        started_at = datetime.now(UTC)
        probe = self.probe(job)
        configuration_sha256 = job_fingerprint(job)
        relative_path = f"source/raw/{probe.content_sha256}/{probe.display_name}"
        artifact = ArtifactRef(
            artifact_id=f"source-{probe.content_sha256[:16]}",
            artifact_type="raw_source",
            content_sha256=probe.content_sha256,
            size_bytes=probe.size_bytes,
            relative_storage_path=relative_path,
        )
        producer = ProducerIdentity(
            kind=ProducerKind.ADAPTER,
            name="local_file",
            version=__version__,
        )
        created_at = datetime.now(UTC)
        artifact_record = ArtifactRecord(
            artifact=artifact,
            input_artifacts=[],
            producer=producer,
            configuration_sha256=configuration_sha256,
            created_at=created_at,
        )
        source_manifest = SourceManifest(
            job_id=job.job_id,
            source_kind=job.source.kind,
            display_name=probe.display_name,
            locator_fingerprint=probe.locator_fingerprint,
            artifact_record=artifact_record,
            created_at=created_at,
        )
        finished_at = datetime.now(UTC)
        stage_manifest = StageManifest(
            job_id=job.job_id,
            stage="acquisition",
            status=StageStatus.SUCCEEDED,
            state_transition=JobState.ACQUIRED,
            input_artifacts=[],
            output_artifacts=[artifact],
            producer=producer,
            configuration_sha256=configuration_sha256,
            started_at=started_at,
            finished_at=finished_at,
        )

        existing_manifest: SourceManifest | None = None
        existing_stage_manifest: StageManifest | None = None
        if workspace.source_manifest_json.exists():
            existing_manifest = read_json_model(
                workspace.source_manifest_json,
                SourceManifest,
                code="SOURCE_MANIFEST_INVALID",
            )
            existing = existing_manifest.artifact_record.artifact
            if existing_manifest.job_id != job.job_id:
                raise integrity_error(
                    "SOURCE_MANIFEST_MISMATCH", "source manifest job does not match"
                )
            if existing_manifest.locator_fingerprint != probe.locator_fingerprint:
                raise integrity_error("SOURCE_LOCATOR_CHANGED", "source locator identity changed")
            if (
                existing.content_sha256 != probe.content_sha256
                or existing.size_bytes != probe.size_bytes
            ):
                raise integrity_error(
                    "SOURCE_CONTENT_CHANGED",
                    "source content changed; create a new job or use a future invalidation workflow",
                )
            source_manifest = existing_manifest
            if workspace.acquisition_manifest_json.exists():
                existing_stage_manifest = read_json_model(
                    workspace.acquisition_manifest_json,
                    StageManifest,
                    code="STAGE_MANIFEST_INVALID",
                )
                if (
                    existing_stage_manifest.job_id != job.job_id
                    or existing_stage_manifest.configuration_sha256 != configuration_sha256
                    or existing_stage_manifest.output_artifacts != [existing]
                    or existing_stage_manifest.status is not StageStatus.SUCCEEDED
                ):
                    raise integrity_error(
                        "STAGE_MANIFEST_MISMATCH",
                        "acquisition stage manifest does not match the source artifact",
                    )
                stage_manifest = existing_stage_manifest

        raw_target = workspace.safe_path(
            source_manifest.artifact_record.artifact.relative_storage_path
        )
        reused = raw_target.exists()
        if reused:
            self._verify_raw(raw_target, source_manifest.artifact_record.artifact)
        else:
            created = atomic_copy_exclusive(self._source_path(job), raw_target)
            if not created:
                reused = True
            self._verify_raw(raw_target, source_manifest.artifact_record.artifact)

        if existing_manifest is None:
            atomic_write_json(workspace.source_manifest_json, source_manifest)
        if existing_stage_manifest is None:
            atomic_write_json(workspace.acquisition_manifest_json, stage_manifest)
        return AcquisitionResult(
            source_manifest=source_manifest,
            stage_manifest=stage_manifest,
            reused=reused,
        )

    @staticmethod
    def _verify_raw(path: Path, artifact: ArtifactRef) -> None:
        try:
            if not path.is_file() or path.stat().st_size != artifact.size_bytes:
                raise integrity_error("ARTIFACT_INTEGRITY_FAILED", "raw artifact size is invalid")
            if sha256_file(path) != artifact.content_sha256:
                raise integrity_error("ARTIFACT_INTEGRITY_FAILED", "raw artifact hash is invalid")
        except OSError as exc:
            raise io_error("ARTIFACT_READ_FAILED", "raw artifact could not be verified") from exc


def verify_acquired_artifact(workspace: JobWorkspace) -> ArtifactRef:
    manifest = read_json_model(
        workspace.source_manifest_json,
        SourceManifest,
        code="SOURCE_MANIFEST_INVALID",
    )
    artifact = manifest.artifact_record.artifact
    target = workspace.safe_path(artifact.relative_storage_path)
    LocalFileAdapter._verify_raw(target, artifact)
    return artifact
