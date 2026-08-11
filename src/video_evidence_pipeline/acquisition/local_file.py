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
    artifact_file_matches,
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _assert_source_path_has_no_links(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            current /= part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise io_error(
                    "SOURCE_NOT_REGULAR_FILE",
                    "local source path must not contain a link or junction",
                )
    except OSError as exc:
        raise io_error("SOURCE_PROBE_FAILED", "local source could not be inspected") from exc


def _validate_source_manifest_contract(manifest: SourceManifest, job: JobSpec) -> ArtifactRef:
    artifact_record = manifest.artifact_record
    artifact = artifact_record.artifact
    configuration_sha256 = job_fingerprint(job)
    expected_display_name = _safe_display_name(Path(job.source.locator))
    expected_relative_path = f"source/raw/{artifact.content_sha256}/{expected_display_name}"
    producer = artifact_record.producer
    if (
        manifest.job_id != job.job_id
        or manifest.source_kind is not SourceKind.LOCAL_FILE
        or manifest.display_name != expected_display_name
        or manifest.locator_fingerprint != local_locator_fingerprint(Path(job.source.locator))
        or artifact.artifact_id != f"source-{artifact.content_sha256[:16]}"
        or artifact.artifact_type != "raw_source"
        or artifact.relative_storage_path != expected_relative_path
        or artifact_record.input_artifacts
        or producer.kind is not ProducerKind.ADAPTER
        or producer.name != "local_file"
        or artifact_record.configuration_sha256 != configuration_sha256
        or artifact_record.redacted_error is not None
        or manifest.created_at != artifact_record.created_at
    ):
        raise integrity_error(
            "SOURCE_MANIFEST_MISMATCH", "source manifest does not match the acquisition contract"
        )
    return artifact


def _validate_stage_manifest_contract(
    stage_manifest: StageManifest,
    source_manifest: SourceManifest,
    job: JobSpec,
) -> None:
    artifact = source_manifest.artifact_record.artifact
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
    ):
        raise integrity_error(
            "STAGE_MANIFEST_MISMATCH",
            "acquisition stage manifest does not match the source artifact",
        )


class LocalFileAdapter(AcquisitionAdapter):
    def _source_path(self, job: JobSpec) -> Path:
        return Path(job.source.locator)

    def probe(self, job: JobSpec) -> ProbeResult:
        if job.source.kind is not SourceKind.LOCAL_FILE:
            raise integrity_error("ADAPTER_KIND_MISMATCH", "local adapter received another kind")
        source = self._source_path(job)
        _assert_source_path_has_no_links(source)
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
            declared_size_bytes=size,
            content_sha256=content_sha256,
        )

    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        started_at = _utc_now()
        probe = self.probe(job)
        configuration_sha256 = job_fingerprint(job)
        if probe.content_sha256 is None or probe.declared_size_bytes is None:
            raise integrity_error("SOURCE_PROBE_FAILED", "local source probe is incomplete")
        relative_path = f"source/raw/{probe.content_sha256}/{probe.display_name}"
        artifact = ArtifactRef(
            artifact_id=f"source-{probe.content_sha256[:16]}",
            artifact_type="raw_source",
            content_sha256=probe.content_sha256,
            size_bytes=probe.declared_size_bytes,
            relative_storage_path=relative_path,
        )
        producer = ProducerIdentity(
            kind=ProducerKind.ADAPTER,
            name="local_file",
            version=__version__,
        )
        existing_manifest: SourceManifest | None = None
        existing_stage_manifest: StageManifest | None = None
        if workspace.source_manifest_json.exists():
            existing_manifest = read_json_model(
                workspace.source_manifest_json,
                SourceManifest,
                code="SOURCE_MANIFEST_INVALID",
            )
            existing = _validate_source_manifest_contract(existing_manifest, job)
            if existing_manifest.created_at > started_at:
                raise integrity_error(
                    "SOURCE_MANIFEST_MISMATCH",
                    "source manifest timestamp is later than this acquisition attempt",
                )
            if existing_manifest.locator_fingerprint != probe.locator_fingerprint:
                raise integrity_error("SOURCE_LOCATOR_CHANGED", "source locator identity changed")
            if (
                existing.content_sha256 != probe.content_sha256
                or existing.size_bytes != probe.declared_size_bytes
            ):
                raise integrity_error(
                    "SOURCE_CONTENT_CHANGED",
                    "source content changed; create a new job or use a future invalidation workflow",
                )
            if workspace.acquisition_manifest_json.exists():
                existing_stage_manifest = read_json_model(
                    workspace.acquisition_manifest_json,
                    StageManifest,
                    code="STAGE_MANIFEST_INVALID",
                )
                _validate_stage_manifest_contract(existing_stage_manifest, existing_manifest, job)

        effective_artifact = (
            existing_manifest.artifact_record.artifact if existing_manifest else artifact
        )
        raw_target = workspace.safe_path(effective_artifact.relative_storage_path)
        reused = raw_target.exists()
        copy_result = None
        if not reused:
            copy_result = atomic_copy_exclusive(
                self._source_path(job),
                raw_target,
                verifier=lambda candidate: self._verify_publication(candidate, effective_artifact),
            )
            reused = not copy_result.created
        if existing_manifest is None:
            self._verify_publication(raw_target, effective_artifact)
        else:
            self._verify_raw(raw_target, effective_artifact)

        if existing_manifest is None:
            created_at = _utc_now()
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
        else:
            source_manifest = existing_manifest

        if existing_stage_manifest is None:
            finished_at = max(_utc_now(), source_manifest.created_at)
            stage_manifest = StageManifest(
                job_id=job.job_id,
                stage="acquisition",
                status=StageStatus.SUCCEEDED,
                state_transition=JobState.ACQUIRED,
                input_artifacts=[],
                output_artifacts=[effective_artifact],
                producer=source_manifest.artifact_record.producer,
                configuration_sha256=configuration_sha256,
                started_at=min(started_at, source_manifest.created_at),
                finished_at=finished_at,
            )
        else:
            stage_manifest = existing_stage_manifest

        _validate_stage_manifest_contract(stage_manifest, source_manifest, job)
        if existing_manifest is None:
            atomic_write_json(workspace.source_manifest_json, source_manifest, private=True)
        if existing_stage_manifest is None:
            atomic_write_json(workspace.acquisition_manifest_json, stage_manifest, private=True)
        return AcquisitionResult(
            source_manifest=source_manifest,
            stage_manifest=stage_manifest,
            reused=reused,
        )

    @staticmethod
    def _verify_raw(path: Path, artifact: ArtifactRef, *, deep: bool = False) -> None:
        try:
            if not artifact_file_matches(path, artifact) or (
                deep and sha256_file(path) != artifact.content_sha256
            ):
                raise integrity_error("ARTIFACT_INTEGRITY_FAILED", "raw artifact is invalid")
        except OSError as exc:
            raise io_error("ARTIFACT_READ_FAILED", "raw artifact could not be verified") from exc

    @staticmethod
    def _verify_publication(path: Path, artifact: ArtifactRef) -> None:
        LocalFileAdapter._verify_raw(path, artifact)
        if sha256_file(path) != artifact.content_sha256:
            raise integrity_error("ARTIFACT_INTEGRITY_FAILED", "raw artifact hash is invalid")


def verify_acquired_bundle(
    workspace: JobWorkspace, job: JobSpec, *, deep: bool = False
) -> ArtifactRef:
    if not workspace.source_manifest_json.is_file():
        raise integrity_error(
            "SOURCE_MANIFEST_INVALID", "ACQUIRED job is missing its source manifest"
        )
    if not workspace.acquisition_manifest_json.is_file():
        raise integrity_error(
            "STAGE_MANIFEST_INVALID", "ACQUIRED job is missing its acquisition stage manifest"
        )
    manifest = read_json_model(
        workspace.source_manifest_json,
        SourceManifest,
        code="SOURCE_MANIFEST_INVALID",
    )
    stage_manifest = read_json_model(
        workspace.acquisition_manifest_json,
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    artifact = _validate_source_manifest_contract(manifest, job)
    _validate_stage_manifest_contract(stage_manifest, manifest, job)
    target = workspace.safe_path(artifact.relative_storage_path)
    LocalFileAdapter._verify_raw(target, artifact, deep=deep)
    return artifact
