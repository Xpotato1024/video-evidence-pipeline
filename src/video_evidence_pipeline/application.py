"""Use cases that orchestrate domain, storage, and adapters."""

from __future__ import annotations

import getpass
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from video_evidence_pipeline.acquisition.base import AcquisitionAdapter, AcquisitionResult
from video_evidence_pipeline.acquisition.bundle import (
    reuse_acquired_network_bundle,
    verify_acquired_bundle,
)
from video_evidence_pipeline.acquisition.registry import adapter_for
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.authoring.pipeline import (
    AuditPrepareResult,
    AuditResult,
    AuthoringPrepareResult,
    DraftResult,
    HumanReviewResult,
    RevisionPrepareResult,
    RevisionResult,
    build_fixed_evidence_bundle,
    commit_audit,
    commit_draft,
    commit_human_review,
    commit_revision,
    load_audit,
    load_audit_request,
    load_authoring_request,
    load_draft,
    load_human_review,
    load_revision,
    load_revision_request,
    prepare_audit_request,
    prepare_authoring_request,
    prepare_revision_request,
    read_audit_response,
    read_authoring_response,
    read_revision_response,
    recover_audit,
    recover_human_review,
    recover_initial_draft,
    recover_revision,
    stage_audit,
    stage_human_review,
    stage_initial_draft,
    stage_revision,
)
from video_evidence_pipeline.authoring.pipeline import (
    cleanup_attempt as cleanup_authoring_attempt,
)
from video_evidence_pipeline.authoring.pipeline import (
    cleanup_stale_attempts as cleanup_stale_authoring_attempts,
)
from video_evidence_pipeline.authoring.pipeline import (
    create_attempt_directory as create_authoring_attempt_directory,
)
from video_evidence_pipeline.delivery import (
    ApprovalResult,
    ExportResult,
    approve_or_recover,
    ensure_approval_confirmation_resolved,
    ensure_approved_draft_marker_free,
    export_or_recover,
    load_approval,
    load_export,
)
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.authoring import (
    AuditDisposition,
    AuditSeverity,
    FixedEvidenceBundle,
)
from video_evidence_pipeline.domain.authorization import AuthorizationRecord
from video_evidence_pipeline.domain.delivery import ReviewerIdentity
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    PrimaryInputKind,
    PrimaryInputSpec,
    SourceKind,
    SourceSpec,
    SupportingInputSpec,
    canonicalize_source_locator,
    job_fingerprint,
    job_to_yaml,
    load_job_yaml,
    primary_input_kind,
    requires_transcript_refinement,
)
from video_evidence_pipeline.domain.manifests import SourceManifest, StageManifest
from video_evidence_pipeline.domain.media import NormalizationConfig
from video_evidence_pipeline.domain.recovery import RecoveryManifest
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.supporting import SupportingInputManifest
from video_evidence_pipeline.domain.transcription import (
    TranscriptionConfig,
    TranscriptManifest,
    TranscriptSegment,
)
from video_evidence_pipeline.domain.transitions import (
    TransitionRecord,
    require_implemented_transition,
    validate_runtime_transition_records,
    validate_transition_fingerprints,
)
from video_evidence_pipeline.errors import (
    VepError,
    configuration_error,
    integrity_error,
    state_error,
)
from video_evidence_pipeline.evidence.pipeline import (
    EvidenceImportResult,
    EvidencePrepareResult,
    commit_staged_evidence,
    load_evidence_request,
    load_evidence_result,
    prepare_evidence_request,
    read_evidence_response,
    recover_evidence_result,
    stage_evidence,
)
from video_evidence_pipeline.evidence.pipeline import cleanup_attempt as cleanup_evidence_attempt
from video_evidence_pipeline.evidence.pipeline import (
    cleanup_stale_attempts as cleanup_stale_evidence_attempts,
)
from video_evidence_pipeline.evidence.pipeline import (
    create_attempt_directory as create_evidence_attempt_directory,
)
from video_evidence_pipeline.media.normalization import (
    NormalizationResult,
    cleanup_attempt,
    cleanup_stale_attempts,
    create_attempt_directory,
    load_normalization_result,
    process_media,
    publish_staged_media,
    publish_transcript_primary,
    recover_normalization_result,
)
from video_evidence_pipeline.media.runner import FFmpegRunner
from video_evidence_pipeline.search import (
    SearchIndexBuildResult,
    SearchResult,
    build_index,
    search_index,
)
from video_evidence_pipeline.storage.atomic import (
    atomic_copy_exclusive,
    atomic_write_json,
    atomic_write_new_bytes,
    read_json_model,
)
from video_evidence_pipeline.storage.jsonl import append_jsonl, exclusive_append_lock, read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.locking import exclusive_file_lock
from video_evidence_pipeline.storage.permissions import ensure_private_directory
from video_evidence_pipeline.supporting import load_supporting_inputs, supporting_fingerprint
from video_evidence_pipeline.transcription.base import AsrBackend
from video_evidence_pipeline.transcription.pipeline import (
    TranscriptionResult,
    load_transcription_result,
    media_bundle_fingerprint,
    prepare_supplied_transcript,
    prepare_transcript,
    publish_transcript,
    recover_transcription_result,
)
from video_evidence_pipeline.transcription.pipeline import (
    cleanup_attempt as cleanup_transcription_attempt,
)
from video_evidence_pipeline.transcription.pipeline import (
    cleanup_stale_attempts as cleanup_stale_transcription_attempts,
)
from video_evidence_pipeline.transcription.pipeline import (
    create_attempt_directory as create_transcription_attempt_directory,
)
from video_evidence_pipeline.transcription.refinement import (
    RefinementPrepareResult,
    RefinementResult,
    evidence_transcript_projection,
    load_refinement_request,
    load_refinement_result,
    prepare_refinement_request,
    publish_refinement,
    read_refinement_response,
)


def _load_context(path: Path) -> tuple[JobWorkspace, JobSpec, list[TransitionRecord], JobState]:
    workspace = JobWorkspace.open(path)
    job = load_job_yaml(workspace.job_yaml)
    if job.job_id != workspace.root.name:
        raise integrity_error("JOB_WORKSPACE_MISMATCH", "job ID does not match workspace name")
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    state = validate_runtime_transition_records(transitions, job.job_id)
    if state is JobState.CREATED:
        validate_transition_fingerprints(transitions, job_fingerprint(job))
    return workspace, job, transitions, state


def initialize_job(
    repository_root: Path,
    *,
    job_id: str,
    source_kind: SourceKind,
    source: str,
    task_type: str,
    language: str,
    output_format: str,
    requirements: list[str] | None = None,
    primary_input: PrimaryInputKind | None = None,
    supporting_inputs: list[SupportingInputSpec] | None = None,
) -> tuple[JobWorkspace, JobSpec]:
    raw_locator = (
        str(Path(source).expanduser().absolute())
        if source_kind is SourceKind.LOCAL_FILE
        else source
    )
    try:
        locator = canonicalize_source_locator(source_kind, raw_locator)
    except ValueError:
        locator = None
    if locator is None:
        raise configuration_error("SOURCE_LOCATOR_INVALID", "source locator is invalid")
    normalized_supports = sorted(
        (
            SupportingInputSpec(
                kind=item.kind,
                locator=str(Path(item.locator).expanduser().absolute()),
            )
            for item in (supporting_inputs or [])
        ),
        key=lambda item: (item.kind.value, item.locator),
    )
    job = JobSpec(
        job_id=job_id,
        created_at=datetime.now(UTC),
        source=SourceSpec(kind=source_kind, locator=locator),
        primary_input=(PrimaryInputSpec(kind=primary_input) if primary_input is not None else None),
        supporting_inputs=normalized_supports,
        assignment=AssignmentSpec(
            task_type=task_type,
            language=language,
            requirements=list(requirements or []),
        ),
        output=OutputSpec(format=output_format),
    )
    workspace = JobWorkspace.create(repository_root, job.job_id)
    atomic_write_new_bytes(
        workspace.job_yaml,
        job_to_yaml(job).encode("utf-8"),
        private=True,
    )
    created = TransitionRecord(
        sequence=1,
        job_id=job.job_id,
        from_state=None,
        to_state=JobState.CREATED,
        trigger="job.init",
        occurred_at=datetime.now(UTC),
        job_fingerprint=job_fingerprint(job),
    )
    append_jsonl(workspace.transitions_jsonl, created)
    return workspace, job


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    workspace: JobWorkspace
    job: JobSpec
    manifest: RecoveryManifest


@dataclass(frozen=True, slots=True)
class _RecoveryParent:
    workspace: JobWorkspace
    job: JobSpec
    authorization: AuthorizationRecord
    source: SourceManifest
    source_stage: StageManifest
    media: NormalizationResult
    transcript: TranscriptionResult
    supporting: SupportingInputManifest | None
    final_draft_sha256: str
    audit_bundle_fingerprint: str
    finding_ids: tuple[str, ...]


def _load_recovery_manifest(workspace: JobWorkspace, job: JobSpec) -> RecoveryManifest:
    manifest = read_json_model(
        workspace.recovery_manifest_json,
        RecoveryManifest,
        code="RECOVERY_MANIFEST_INVALID",
    )
    if manifest.child_job_id != job.job_id or manifest.child_job_fingerprint != job_fingerprint(
        job
    ):
        raise integrity_error(
            "RECOVERY_MANIFEST_MISMATCH",
            "recovery manifest does not match the child job",
        )
    return manifest


def _recovery_media_artifacts(media) -> tuple[ArtifactRef, ...]:
    return tuple(
        [
            *([media.analysis_proxy.artifact] if media.analysis_proxy is not None else []),
            *([media.speech_audio.artifact] if media.speech_audio is not None else []),
            *(
                artifact
                for subtitle in media.subtitles
                for artifact in (subtitle.webvtt_artifact, subtitle.cues_artifact)
                if artifact is not None
            ),
            *(
                record.artifact
                for record in (media.frame_index.records if media.frame_index is not None else [])
            ),
            *([media.frame_index_artifact] if media.frame_index_artifact is not None else []),
        ]
    )


def _recovery_supporting_artifacts(supporting) -> tuple[ArtifactRef, ...]:
    return tuple(
        artifact
        for record in supporting.inputs
        for artifact in (
            record.original_artifact,
            record.canonical_text_artifact,
            *(page.artifact for page in record.rendered_pages),
        )
        if artifact is not None
    )


def _verify_recovery_copy(path: Path, artifact: ArtifactRef) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != artifact.size_bytes
            or sha256_file(path) != artifact.content_sha256
        ):
            raise integrity_error(
                "RECOVERY_COPY_INTEGRITY_FAILED",
                "recovered artifact does not match its stored identity",
            )
    except OSError as exc:
        raise integrity_error(
            "RECOVERY_COPY_INTEGRITY_FAILED",
            "recovered artifact is unavailable",
        ) from exc


def _copy_recovery_artifacts(
    parent: JobWorkspace,
    child: JobWorkspace,
    artifacts: tuple[ArtifactRef, ...],
) -> None:
    copied: set[str] = set()
    for artifact in artifacts:
        if artifact.relative_storage_path in copied:
            continue
        copied.add(artifact.relative_storage_path)
        source = parent.safe_path(artifact.relative_storage_path)
        target = child.safe_path(artifact.relative_storage_path)
        directory = child.root
        for component in target.parent.relative_to(child.root).parts:
            directory /= component
            ensure_private_directory(directory)
        result = atomic_copy_exclusive(
            source,
            target,
            verifier=lambda candidate, artifact=artifact: _verify_recovery_copy(
                candidate, artifact
            ),
        )
        if not result.created:
            _verify_recovery_copy(target, artifact)


def _recovery_parent(
    parent_path: Path,
) -> _RecoveryParent:
    workspace, job, transitions, state = _load_context(parent_path)
    if (
        state is not JobState.BLOCKED
        or not transitions
        or (
            transitions[-1].from_state is not JobState.AUDITED
            or transitions[-1].to_state is not JobState.BLOCKED
        )
    ):
        raise state_error(
            "RECOVERY_PARENT_NOT_ELIGIBLE",
            "parent is not an audited blocked job",
        )
    _validate_job_unmapped(parent_path)
    authorization = _authorization(workspace)
    _fixed, history, _human_review = _load_authoring_state_integrity(workspace, job, state)
    draft, audit = history[-1]
    if (
        audit is None
        or audit.manifest.disposition is not AuditDisposition.BLOCKED
        or draft.manifest.revision_count != 2
    ):
        raise state_error(
            "RECOVERY_PARENT_NOT_ELIGIBLE",
            "parent did not reach the revision limit through a blocked audit",
        )
    material_findings = tuple(
        finding
        for finding in audit.findings
        if finding.severity in {AuditSeverity.P0, AuditSeverity.P1}
    )
    if not material_findings or any(
        finding.category != "content-integrity" for finding in material_findings
    ):
        raise state_error(
            "RECOVERY_PARENT_NOT_ELIGIBLE",
            "parent does not have only material content-integrity findings",
        )
    raw_artifact = verify_acquired_bundle(workspace, job)
    media = load_normalization_result(workspace, job, raw_artifact)
    transcript = load_transcription_result(workspace, job, media.media_manifest)
    source_manifest = read_json_model(
        workspace.source_manifest_json,
        SourceManifest,
        code="SOURCE_MANIFEST_INVALID",
    )
    source_stage = read_json_model(
        workspace.acquisition_manifest_json,
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    supporting = load_supporting_inputs(workspace, job) if job.supporting_inputs else None
    return _RecoveryParent(
        workspace=workspace,
        job=job,
        authorization=authorization,
        source=source_manifest,
        source_stage=source_stage,
        media=media,
        transcript=transcript,
        supporting=supporting,
        final_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        audit_bundle_fingerprint=audit.manifest.bundle_fingerprint,
        finding_ids=tuple(sorted(finding.finding_id for finding in material_findings)),
    )


def recover_job(
    parent_path: Path,
    new_job_id: str,
    *,
    confirm: bool,
    basis: str,
) -> RecoveryResult:
    """Fork only an eligible blocked job at its raw-transcript reuse boundary."""

    if not confirm:
        raise state_error(
            "RECOVERY_CONFIRMATION_REQUIRED",
            "recovery requires explicit --confirm",
        )
    qualified = _recovery_parent(parent_path)
    if new_job_id == qualified.job.job_id:
        raise configuration_error(
            "RECOVERY_CHILD_ID_INVALID",
            "recovery child job ID must differ from its parent",
        )
    child_data = qualified.job.model_dump(mode="python")
    child_data.update({"job_id": new_job_id, "created_at": datetime.now(UTC)})
    child_job = JobSpec.model_validate(child_data)
    child_fingerprint = job_fingerprint(child_job)
    recovery = RecoveryManifest(
        parent_job_id=qualified.job.job_id,
        parent_job_fingerprint=job_fingerprint(qualified.job),
        parent_final_draft_sha256=qualified.final_draft_sha256,
        parent_audit_bundle_fingerprint=qualified.audit_bundle_fingerprint,
        parent_finding_ids=list(qualified.finding_ids),
        recovery_basis=basis,
        child_job_id=child_job.job_id,
        child_job_fingerprint=child_fingerprint,
        created_at=datetime.now(UTC),
    )
    child = JobWorkspace.create(qualified.workspace.jobs_root.parent.parent, child_job.job_id)
    atomic_write_new_bytes(child.job_yaml, job_to_yaml(child_job).encode("utf-8"), private=True)
    append_jsonl(
        child.transitions_jsonl,
        TransitionRecord(
            sequence=1,
            job_id=child_job.job_id,
            from_state=None,
            to_state=JobState.CREATED,
            trigger="job.init",
            occurred_at=datetime.now(UTC),
            job_fingerprint=child_fingerprint,
        ),
    )

    child_source = qualified.source.model_copy(
        update={
            "job_id": child_job.job_id,
            "artifact_record": qualified.source.artifact_record.model_copy(
                update={"configuration_sha256": child_fingerprint}
            ),
        }
    )
    child_source_stage = qualified.source_stage.model_copy(
        update={"job_id": child_job.job_id, "configuration_sha256": child_fingerprint}
    )
    child_media = qualified.media.media_manifest.model_copy(update={"job_id": child_job.job_id})
    child_media_stage = qualified.media.stage_manifest.model_copy(
        update={"job_id": child_job.job_id}
    )
    child_transcript = qualified.transcript.transcript_manifest.model_copy(
        update={"job_id": child_job.job_id}
    )
    child_transcript_stage = qualified.transcript.stage_manifest.model_copy(
        update={"job_id": child_job.job_id}
    )
    child_supporting = (
        qualified.supporting.model_copy(
            update={
                "job_id": child_job.job_id,
                "supporting_fingerprint": supporting_fingerprint(
                    child_job, qualified.supporting.inputs
                ),
            }
        )
        if qualified.supporting is not None
        else None
    )
    _copy_recovery_artifacts(
        qualified.workspace,
        child,
        (
            qualified.source.artifact_record.artifact,
            *_recovery_media_artifacts(qualified.media.media_manifest),
            qualified.transcript.transcript_manifest.segments_artifact,
            *(
                _recovery_supporting_artifacts(qualified.supporting)
                if qualified.supporting is not None
                else ()
            ),
        ),
    )
    atomic_write_json(child.source_manifest_json, child_source, private=True)
    atomic_write_json(child.acquisition_manifest_json, child_source_stage, private=True)
    atomic_write_json(child.media_manifest_json, child_media, private=True)
    atomic_write_json(child.normalization_manifest_json, child_media_stage, private=True)
    atomic_write_json(child.transcript_manifest_json, child_transcript, private=True)
    atomic_write_json(child.transcription_stage_manifest_json, child_transcript_stage, private=True)
    if child_supporting is not None:
        atomic_write_json(child.supporting_manifest_json, child_supporting, private=True)
    atomic_write_json(child.recovery_manifest_json, recovery, private=True)

    authorization = AuthorizationRecord(
        job_id=child_job.job_id,
        job_fingerprint=child_fingerprint,
        confirmed_by_user=True,
        authorization_basis=basis,
        allow_local_processing=qualified.authorization.allow_local_processing,
        allow_network_acquisition=qualified.authorization.allow_network_acquisition,
        allow_external_api=qualified.authorization.allow_external_api,
        confirmed_at=datetime.now(UTC),
        confirmer_identity=getpass.getuser() or "local-user",
    )
    atomic_write_json(child.authorization_json, authorization, private=True)
    with exclusive_append_lock(child.transitions_jsonl):
        _append_transition_locked(
            child,
            child_job,
            from_state=JobState.CREATED,
            to_state=JobState.AUTHORIZED,
            trigger="recovery.authorize",
        )
        _append_transition_locked(
            child,
            child_job,
            from_state=JobState.AUTHORIZED,
            to_state=JobState.ACQUIRED,
            trigger="recovery.reuse-source",
        )
        _append_transition_locked(
            child,
            child_job,
            from_state=JobState.ACQUIRED,
            to_state=JobState.NORMALIZED,
            trigger="recovery.reuse-normalization",
        )
        _append_transition_locked(
            child,
            child_job,
            from_state=JobState.NORMALIZED,
            to_state=JobState.TRANSCRIBED,
            trigger="recovery.reuse-transcript",
        )
    _load_recovery_manifest(child, child_job)
    validate_job(child.root)
    return RecoveryResult(workspace=child, job=child_job, manifest=recovery)


def _validate_job_unmapped(path: Path) -> dict[str, Any]:
    workspace, job, transitions, state = _load_context(path)
    if workspace.recovery_manifest_json.exists():
        _load_recovery_manifest(workspace, job)
    if state is not JobState.CREATED:
        authorization = _authorization(workspace)
        _validate_authorization_binding(job, transitions, authorization)
        _require_current_authorization_fingerprint(job, authorization)
    authoring_states = {
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.HUMAN_REVIEW_READY,
        JobState.BLOCKED,
        JobState.HUMAN_APPROVED,
        JobState.EXPORTED,
    }
    refinement_states = {JobState.TRANSCRIPT_REFINED, JobState.EVIDENCE_READY, *authoring_states}
    acquired_states = {
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.TRANSCRIPT_REFINED,
        JobState.EVIDENCE_READY,
        *authoring_states,
    }
    normalized_states = {
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.TRANSCRIPT_REFINED,
        JobState.EVIDENCE_READY,
        *authoring_states,
    }
    if state in acquired_states:
        raw_artifact = verify_acquired_bundle(workspace, job)
        if state in normalized_states:
            media_manifest = load_normalization_result(workspace, job, raw_artifact).media_manifest
            if state in {JobState.TRANSCRIBED, *refinement_states}:
                transcription = load_transcription_result(workspace, job, media_manifest)
                evidence_manifest = transcription.transcript_manifest
                evidence_segments = transcription.segments
                if state in refinement_states:
                    evidence_manifest, evidence_segments = _evidence_transcript_inputs(
                        workspace, job, media_manifest, transcription
                    )
                if state in {JobState.EVIDENCE_READY, *authoring_states}:
                    load_evidence_result(
                        workspace,
                        job,
                        media_manifest,
                        evidence_manifest,
                        evidence_segments,
                    )
                    if state in authoring_states:
                        try:
                            fixed, history, human_review = _load_authoring_state_integrity(
                                workspace, job, state
                            )
                        except VepError as exc:
                            if state not in {JobState.HUMAN_APPROVED, JobState.EXPORTED}:
                                raise
                            raise integrity_error(
                                "APPROVAL_STALE",
                                "approval no longer matches verified upstream state",
                            ) from exc
                        if state in {JobState.HUMAN_APPROVED, JobState.EXPORTED}:
                            draft, audit = history[-1]
                            if audit is None or human_review is None:
                                raise integrity_error(
                                    "APPROVAL_STALE", "approval upstream is incomplete"
                                )
                            approval = load_approval(workspace, job, draft, audit, human_review)
                            if state is JobState.EXPORTED:
                                load_export(workspace, job, fixed, approval)
    return {
        "valid": True,
        "job_id": job.job_id,
        "job_fingerprint": job_fingerprint(job),
        "state": state.value,
        "workspace": workspace.root.name,
    }


def validate_job(path: Path) -> dict[str, Any]:
    _workspace, _job, _transitions, state = _load_context(path)
    try:
        return _validate_job_unmapped(path)
    except VepError as exc:
        if (
            state in {JobState.HUMAN_APPROVED, JobState.EXPORTED}
            and exc.code != "APPROVAL_STALE"
            and not exc.code.startswith("EXPORT_")
        ):
            raise integrity_error(
                "APPROVAL_STALE",
                "approval no longer matches verified upstream state",
            ) from exc
        raise


def authorize_job(
    path: Path,
    *,
    confirm: bool,
    basis: str,
    allow_local_processing: bool,
    allow_network_acquisition: bool = False,
    allow_external_api: bool,
    confirmer_identity: str | None,
) -> AuthorizationRecord:
    if not confirm:
        raise state_error(
            "AUTHORIZATION_CONFIRMATION_REQUIRED",
            "authorization requires explicit --confirm",
        )
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, _, state = _load_context(path)
        if state is not JobState.CREATED:
            raise state_error("STATE_GATE_FAILED", "job cannot be authorized in its current state")
        record = AuthorizationRecord(
            job_id=job.job_id,
            job_fingerprint=job_fingerprint(job),
            confirmed_by_user=True,
            authorization_basis=basis,
            allow_local_processing=allow_local_processing,
            allow_network_acquisition=allow_network_acquisition,
            allow_external_api=allow_external_api,
            confirmed_at=datetime.now(UTC),
            confirmer_identity=confirmer_identity or getpass.getuser() or "local-user",
        )
        atomic_write_json(workspace.authorization_json, record, private=True)
        _append_transition_locked(
            workspace,
            job,
            from_state=JobState.CREATED,
            to_state=JobState.AUTHORIZED,
            trigger="job.authorize",
        )
        return record


def _authorization(workspace: JobWorkspace) -> AuthorizationRecord:
    return read_json_model(
        workspace.authorization_json,
        AuthorizationRecord,
        code="AUTHORIZATION_INVALID",
    )


def _validate_authorization_binding(
    job: JobSpec,
    transitions: list[TransitionRecord],
    authorization: AuthorizationRecord,
) -> None:
    if authorization.job_id != job.job_id:
        raise integrity_error(
            "AUTHORIZATION_JOB_MISMATCH",
            "authorization belongs to another job",
        )
    validate_transition_fingerprints(transitions, authorization.job_fingerprint)


def _require_current_authorization(
    job: JobSpec,
    transitions: list[TransitionRecord],
    authorization: AuthorizationRecord,
) -> None:
    _validate_authorization_binding(job, transitions, authorization)
    _require_current_authorization_fingerprint(job, authorization)
    _require_local_processing(authorization)


def _require_current_authorization_fingerprint(
    job: JobSpec,
    authorization: AuthorizationRecord,
) -> None:
    if authorization.job_fingerprint != job_fingerprint(job):
        raise state_error(
            "AUTHORIZATION_STALE",
            "authorization no longer matches the normalized job specification",
        )


def _require_local_processing(authorization: AuthorizationRecord) -> None:
    if not authorization.allow_local_processing:
        raise state_error(
            "LOCAL_PROCESSING_NOT_AUTHORIZED",
            "local processing was not explicitly authorized",
        )


def _require_network_acquisition(job: JobSpec, authorization: AuthorizationRecord) -> None:
    if job.source.kind is SourceKind.LOCAL_FILE:
        return
    if not authorization.allow_network_acquisition:
        raise state_error(
            "NETWORK_ACQUISITION_NOT_AUTHORIZED",
            "network acquisition was not explicitly authorized",
        )


def acquire_job(
    path: Path,
    runtime_config: AcquisitionRuntimeConfig | None = None,
    adapter_override: AcquisitionAdapter | None = None,
) -> AcquisitionResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in {JobState.AUTHORIZED, JobState.ACQUIRED}:
            raise state_error("STATE_GATE_FAILED", "job must be AUTHORIZED before acquisition")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        _require_network_acquisition(job, authorization)
        runtime = runtime_config or AcquisitionRuntimeConfig()
        runtime.validate_for(job.source.kind)
        if state is JobState.ACQUIRED:
            verify_acquired_bundle(workspace, job)
            if job.source.kind is not SourceKind.LOCAL_FILE:
                reused = reuse_acquired_network_bundle(workspace, job)
                if reused is None:
                    raise integrity_error(
                        "SOURCE_MANIFEST_INVALID", "network acquired bundle is incomplete"
                    )
                return reused
        elif job.source.kind is not SourceKind.LOCAL_FILE:
            recovered = reuse_acquired_network_bundle(workspace, job)
            if recovered is not None:
                _append_transition_locked(
                    workspace,
                    job,
                    from_state=JobState.AUTHORIZED,
                    to_state=JobState.ACQUIRED,
                    trigger=f"acquire.{job.source.kind.value}",
                )
                return recovered
        selected_adapter = adapter_override or adapter_for(job.source, runtime)
        result = selected_adapter.acquire(job, workspace)
        if state is JobState.AUTHORIZED:
            _append_transition_locked(
                workspace,
                job,
                from_state=JobState.AUTHORIZED,
                to_state=JobState.ACQUIRED,
                trigger=f"acquire.{job.source.kind.value}",
            )
        return result


def normalize_job(
    path: Path,
    config: NormalizationConfig | None = None,
    runner_override: FFmpegRunner | None = None,
) -> NormalizationResult:
    normalization_config = config or NormalizationConfig()
    workspace = JobWorkspace.open(path)
    with exclusive_file_lock(
        workspace.normalization_lock_path,
        busy_code="NORMALIZATION_BUSY",
        busy_message="media normalization is already running",
        failure_code="NORMALIZATION_LOCK_FAILED",
        failure_message="media normalization could not be locked",
    ):
        cleanup_stale_attempts(workspace)
        return _normalize_job_locked(path, normalization_config, runner_override)


def _normalize_job_locked(
    path: Path,
    normalization_config: NormalizationConfig,
    runner_override: FFmpegRunner | None,
) -> NormalizationResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in {JobState.ACQUIRED, JobState.NORMALIZED}:
            raise state_error("STATE_GATE_FAILED", "job must be ACQUIRED before normalization")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        raw_artifact = verify_acquired_bundle(workspace, job)
        if state is JobState.NORMALIZED:
            return load_normalization_result(
                workspace,
                job,
                raw_artifact,
                expected_config=normalization_config,
            )
        recovered = recover_normalization_result(
            workspace,
            job,
            raw_artifact,
            expected_config=normalization_config,
        )
        if recovered is not None:
            _append_transition_locked(
                workspace,
                job,
                from_state=JobState.ACQUIRED,
                to_state=JobState.NORMALIZED,
                trigger="normalize.ffmpeg",
            )
            return recovered
        if primary_input_kind(job) is PrimaryInputKind.TRANSCRIPT:
            result = publish_transcript_primary(
                workspace,
                job,
                raw_artifact,
                normalization_config,
            )
            _append_transition_locked(
                workspace,
                job,
                from_state=JobState.ACQUIRED,
                to_state=JobState.NORMALIZED,
                trigger="normalize.transcript",
            )
            return result
        snapshot_sequence = transitions[-1].sequence
        snapshot_raw_artifact = raw_artifact
        source_path = workspace.safe_path(raw_artifact.relative_storage_path)

    runner = runner_override or FFmpegRunner()
    ffmpeg_identity, ffprobe_identity = runner.discover_identities(
        timeout_seconds=normalization_config.tool_timeout_seconds,
        output_limit_bytes=normalization_config.stdout_limit_bytes,
    )
    runner.pin_identities(ffmpeg_identity, ffprobe_identity)
    attempt = create_attempt_directory(workspace)
    operation_failed = False
    try:
        staged = process_media(
            runner,
            source_path,
            snapshot_raw_artifact,
            normalization_config,
            attempt,
            ffmpeg_identity,
            ffprobe_identity,
            primary_input_kind=primary_input_kind(job),
        )
        with exclusive_append_lock(workspace.transitions_jsonl):
            workspace, job, transitions, state = _load_context(path)
            current_raw_artifact = verify_acquired_bundle(workspace, job)
            if state is JobState.NORMALIZED:
                winner = load_normalization_result(
                    workspace,
                    job,
                    current_raw_artifact,
                    expected_config=normalization_config,
                )
                if (
                    winner.media_manifest.normalization_fingerprint
                    != staged.normalization_fingerprint
                ):
                    raise integrity_error(
                        "NORMALIZATION_FINGERPRINT_MISMATCH",
                        "concurrent normalization used a different tool fingerprint",
                    )
                return winner
            if (
                state is not JobState.ACQUIRED
                or transitions[-1].sequence != snapshot_sequence
                or current_raw_artifact != snapshot_raw_artifact
            ):
                raise state_error(
                    "NORMALIZATION_SNAPSHOT_STALE",
                    "job or raw source changed while media tools were running",
                )
            recovered = recover_normalization_result(
                workspace,
                job,
                current_raw_artifact,
                expected_config=normalization_config,
            )
            if recovered is not None:
                _append_transition_locked(
                    workspace,
                    job,
                    from_state=JobState.ACQUIRED,
                    to_state=JobState.NORMALIZED,
                    trigger="normalize.ffmpeg",
                )
                return recovered
            runner.revalidate_pinned_identities(ffmpeg_identity, ffprobe_identity)
            result = publish_staged_media(
                workspace,
                job,
                current_raw_artifact,
                normalization_config,
                staged,
            )
            _append_transition_locked(
                workspace,
                job,
                from_state=JobState.ACQUIRED,
                to_state=JobState.NORMALIZED,
                trigger="normalize.ffmpeg",
            )
            return result
    except Exception:
        operation_failed = True
        raise
    finally:
        try:
            cleanup_attempt(workspace, attempt)
        except VepError:
            if not operation_failed:
                raise


def transcribe_job(
    path: Path,
    config: TranscriptionConfig | None = None,
    backend_override: AsrBackend | None = None,
    *,
    asr_model_directory: Path | None = None,
) -> TranscriptionResult:
    transcription_config = config or TranscriptionConfig()
    workspace = JobWorkspace.open(path)
    with exclusive_file_lock(
        workspace.transcription_lock_path,
        busy_code="TRANSCRIPTION_BUSY",
        busy_message="transcription is already running",
        failure_code="TRANSCRIPTION_LOCK_FAILED",
        failure_message="transcription could not be locked",
    ):
        cleanup_stale_transcription_attempts(workspace)
        return _transcribe_job_locked(
            path,
            transcription_config,
            backend_override,
            asr_model_directory=asr_model_directory,
        )


def _transcribe_job_locked(
    path: Path,
    config: TranscriptionConfig,
    backend_override: AsrBackend | None,
    *,
    asr_model_directory: Path | None,
) -> TranscriptionResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in {JobState.NORMALIZED, JobState.TRANSCRIBED}:
            raise state_error("STATE_GATE_FAILED", "job must be NORMALIZED before transcription")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        raw_artifact = verify_acquired_bundle(workspace, job)
        media_result = load_normalization_result(workspace, job, raw_artifact)
        media_manifest = media_result.media_manifest
        if state is JobState.TRANSCRIBED:
            return load_transcription_result(
                workspace,
                job,
                media_manifest,
                expected_config=config,
            )
        recovered = recover_transcription_result(
            workspace,
            job,
            media_manifest,
            expected_config=config,
        )
        if recovered is not None:
            _append_transition_locked(
                workspace,
                job,
                from_state=JobState.NORMALIZED,
                to_state=JobState.TRANSCRIBED,
                trigger="transcribe.local",
            )
            return recovered
        snapshot_sequence = transitions[-1].sequence
        snapshot_raw_artifact = raw_artifact
        snapshot_media_fingerprint = media_bundle_fingerprint(media_manifest)

    attempt = create_transcription_attempt_directory(workspace)
    operation_failed = False
    try:
        if primary_input_kind(job) is PrimaryInputKind.TRANSCRIPT:
            if backend_override is not None or asr_model_directory is not None:
                raise configuration_error(
                    "SUPPLIED_TRANSCRIPT_ASR_FORBIDDEN",
                    "supplied transcript input does not permit ASR configuration",
                )
            prepared = prepare_supplied_transcript(workspace, job, media_manifest, config)
        else:
            prepared = prepare_transcript(
                workspace,
                job,
                media_manifest,
                config,
                backend_override,
                model_directory=asr_model_directory,
            )
        with exclusive_append_lock(workspace.transitions_jsonl):
            workspace, job, transitions, state = _load_context(path)
            current_raw_artifact = verify_acquired_bundle(workspace, job)
            current_media_result = load_normalization_result(workspace, job, current_raw_artifact)
            current_media_manifest = current_media_result.media_manifest
            if state is JobState.TRANSCRIBED:
                winner = load_transcription_result(
                    workspace,
                    job,
                    current_media_manifest,
                    expected_config=config,
                )
                if (
                    winner.transcript_manifest.transcription_fingerprint
                    != prepared.transcription_fingerprint
                ):
                    raise integrity_error(
                        "TRANSCRIPTION_FINGERPRINT_MISMATCH",
                        "concurrent transcription used a different fingerprint",
                    )
                return winner
            if (
                state is not JobState.NORMALIZED
                or transitions[-1].sequence != snapshot_sequence
                or current_raw_artifact != snapshot_raw_artifact
                or media_bundle_fingerprint(current_media_manifest) != snapshot_media_fingerprint
            ):
                raise state_error(
                    "TRANSCRIPTION_SNAPSHOT_STALE",
                    "job or normalized media changed while transcription was running",
                )
            recovered = recover_transcription_result(
                workspace,
                job,
                current_media_manifest,
                expected_config=config,
            )
            if recovered is not None:
                _append_transition_locked(
                    workspace,
                    job,
                    from_state=JobState.NORMALIZED,
                    to_state=JobState.TRANSCRIBED,
                    trigger="transcribe.local",
                )
                return recovered
            result = publish_transcript(
                workspace,
                job,
                current_media_manifest,
                prepared,
                attempt,
            )
            _append_transition_locked(
                workspace,
                job,
                from_state=JobState.NORMALIZED,
                to_state=JobState.TRANSCRIBED,
                trigger="transcribe.local",
            )
            return result
    except Exception:
        operation_failed = True
        raise
    finally:
        try:
            cleanup_transcription_attempt(workspace, attempt)
        except VepError:
            if not operation_failed:
                raise


def _evidence_transcript_inputs(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest,
    transcription: TranscriptionResult,
) -> tuple:
    """Select raw v1 transcript or the explicit refined projection for downstream stages."""

    if not requires_transcript_refinement(job):
        return transcription.transcript_manifest, transcription.segments
    refinement = load_refinement_result(
        workspace,
        job,
        media_manifest,
        transcription.transcript_manifest,
        transcription.segments,
    )
    return evidence_transcript_projection(
        transcription.transcript_manifest, refinement
    ), refinement.segments


def _evidence_predecessor(job: JobSpec) -> JobState:
    """Keep v1 jobs on their historical direct edge to evidence."""

    return (
        JobState.TRANSCRIPT_REFINED if requires_transcript_refinement(job) else JobState.TRANSCRIBED
    )


def prepare_transcript_refinement_job(path: Path) -> RefinementPrepareResult:
    workspace = JobWorkspace.open(path)
    with (
        exclusive_file_lock(
            workspace.refinement_lock_path,
            busy_code="REFINEMENT_BUSY",
            busy_message="transcript refinement is already running",
            failure_code="REFINEMENT_LOCK_FAILED",
            failure_message="transcript refinement could not be locked",
        ),
        exclusive_append_lock(workspace.transitions_jsonl),
    ):
        workspace, job, transitions, state = _load_context(path)
        if not requires_transcript_refinement(job):
            raise state_error(
                "REFINEMENT_NOT_REQUIRED", "legacy video jobs continue directly to evidence"
            )
        if state not in {JobState.TRANSCRIBED, JobState.TRANSCRIPT_REFINED}:
            raise state_error(
                "STATE_GATE_FAILED", "job must be TRANSCRIBED before transcript refinement"
            )
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        raw_artifact = verify_acquired_bundle(workspace, job)
        media_manifest = load_normalization_result(workspace, job, raw_artifact).media_manifest
        transcription = load_transcription_result(workspace, job, media_manifest)
        request = prepare_refinement_request(
            workspace,
            job,
            media_manifest,
            transcription.transcript_manifest,
            transcription.segments,
        )
        if state is JobState.TRANSCRIPT_REFINED:
            load_refinement_result(
                workspace,
                job,
                media_manifest,
                transcription.transcript_manifest,
                transcription.segments,
                request=request.request,
            )
        return request


def import_transcript_refinement_job(
    path: Path,
    response_path: Path,
    *,
    _allow_fixture: bool = False,
) -> RefinementResult:
    workspace = JobWorkspace.open(path)
    with (
        exclusive_file_lock(
            workspace.refinement_lock_path,
            busy_code="REFINEMENT_BUSY",
            busy_message="transcript refinement is already running",
            failure_code="REFINEMENT_LOCK_FAILED",
            failure_message="transcript refinement could not be locked",
        ),
        exclusive_append_lock(workspace.transitions_jsonl),
    ):
        workspace, job, transitions, state = _load_context(path)
        if not requires_transcript_refinement(job):
            raise state_error(
                "REFINEMENT_NOT_REQUIRED", "legacy video jobs continue directly to evidence"
            )
        if state not in {JobState.TRANSCRIBED, JobState.TRANSCRIPT_REFINED}:
            raise state_error(
                "STATE_GATE_FAILED", "job must be TRANSCRIBED before transcript refinement"
            )
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        raw_artifact = verify_acquired_bundle(workspace, job)
        media_manifest = load_normalization_result(workspace, job, raw_artifact).media_manifest
        transcription = load_transcription_result(workspace, job, media_manifest)
        request = load_refinement_request(
            workspace,
            job,
            media_manifest,
            transcription.transcript_manifest,
            transcription.segments,
        )
        response = read_refinement_response(response_path, request, allow_fixture=_allow_fixture)
        if response.refiner.external_api_used and not authorization.allow_external_api:
            raise state_error(
                "EXTERNAL_API_NOT_AUTHORIZED",
                "refinement response used an external API without job authorization",
            )
        if state is JobState.TRANSCRIPT_REFINED:
            existing = load_refinement_result(
                workspace,
                job,
                media_manifest,
                transcription.transcript_manifest,
                transcription.segments,
                request=request,
            )
            if existing.refinement_manifest.response_fingerprint != fingerprint(response):
                raise integrity_error(
                    "REFINEMENT_FINGERPRINT_MISMATCH",
                    "refinement response does not match the committed bundle",
                )
            return existing
        result = publish_refinement(
            workspace,
            job,
            transcription.transcript_manifest,
            transcription.segments,
            request,
            response,
        )
        _append_transition_locked(
            workspace,
            job,
            from_state=JobState.TRANSCRIBED,
            to_state=JobState.TRANSCRIPT_REFINED,
            trigger="transcript.refine.import",
        )
        return result


def _import_transcript_refinement_fixture(path: Path, response_path: Path) -> RefinementResult:
    """Internal test-only hook; production CLI never exposes this route."""

    return import_transcript_refinement_job(path, response_path, _allow_fixture=True)


def prepare_evidence_job(path: Path) -> EvidencePrepareResult:
    workspace = JobWorkspace.open(path)
    with (
        exclusive_file_lock(
            workspace.evidence_lock_path,
            busy_code="EVIDENCE_BUSY",
            busy_message="evidence preparation or import is already running",
            failure_code="EVIDENCE_LOCK_FAILED",
            failure_message="evidence stage could not be locked",
        ),
        exclusive_append_lock(workspace.transitions_jsonl),
    ):
        workspace, job, transitions, state = _load_context(path)
        predecessor = _evidence_predecessor(job)
        if state not in {predecessor, JobState.EVIDENCE_READY}:
            raise state_error(
                "STATE_GATE_FAILED",
                "job must complete its transcript stage before evidence preparation",
            )
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        raw_artifact = verify_acquired_bundle(workspace, job)
        media_result = load_normalization_result(workspace, job, raw_artifact)
        media_manifest = media_result.media_manifest
        transcription = load_transcription_result(workspace, job, media_manifest)
        evidence_transcript_manifest, evidence_segments = _evidence_transcript_inputs(
            workspace, job, media_manifest, transcription
        )
        if state is JobState.EVIDENCE_READY:
            load_evidence_result(
                workspace,
                job,
                media_manifest,
                evidence_transcript_manifest,
                evidence_segments,
            )
        return prepare_evidence_request(
            workspace,
            job,
            media_manifest,
            evidence_transcript_manifest,
            evidence_segments,
        )


def import_evidence_job(
    path: Path,
    response_path: Path,
    *,
    _allow_fixture: bool = False,
) -> EvidenceImportResult:
    workspace = JobWorkspace.open(path)
    with exclusive_file_lock(
        workspace.evidence_lock_path,
        busy_code="EVIDENCE_BUSY",
        busy_message="evidence preparation or import is already running",
        failure_code="EVIDENCE_LOCK_FAILED",
        failure_message="evidence stage could not be locked",
    ):
        cleanup_stale_evidence_attempts(workspace)
        return _import_evidence_job_locked(
            path,
            response_path,
            allow_fixture=_allow_fixture,
        )


def _import_evidence_fixture(path: Path, response_path: Path) -> EvidenceImportResult:
    """Internal test-only hook; production CLI never exposes this route."""
    return import_evidence_job(path, response_path, _allow_fixture=True)


def _import_evidence_job_locked(
    path: Path,
    response_path: Path,
    *,
    allow_fixture: bool,
) -> EvidenceImportResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        predecessor = _evidence_predecessor(job)
        if state not in {predecessor, JobState.EVIDENCE_READY}:
            raise state_error(
                "STATE_GATE_FAILED", "job must complete its transcript stage before evidence import"
            )
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        snapshot_state = state
        snapshot_sequence = transitions[-1].sequence
        snapshot_job = job
        snapshot_authorization = authorization

    raw_artifact = verify_acquired_bundle(workspace, snapshot_job)
    media_result = load_normalization_result(workspace, snapshot_job, raw_artifact)
    media_manifest = media_result.media_manifest
    transcription = load_transcription_result(workspace, snapshot_job, media_manifest)
    evidence_transcript_manifest, evidence_segments = _evidence_transcript_inputs(
        workspace, snapshot_job, media_manifest, transcription
    )
    request = load_evidence_request(
        workspace,
        snapshot_job,
        media_manifest,
        evidence_transcript_manifest,
        evidence_segments,
    )

    response = read_evidence_response(
        response_path,
        request,
        allow_fixture=allow_fixture,
    )
    if response.analyzer.external_api_used and not snapshot_authorization.allow_external_api:
        raise state_error(
            "EXTERNAL_API_NOT_AUTHORIZED",
            "evidence response used an external API without job authorization",
        )
    response_fingerprint = fingerprint(response)
    if snapshot_state is JobState.EVIDENCE_READY:
        existing = load_evidence_result(
            workspace,
            snapshot_job,
            media_manifest,
            evidence_transcript_manifest,
            evidence_segments,
        )
        if existing.evidence_manifest.response_fingerprint != response_fingerprint:
            raise integrity_error(
                "EVIDENCE_FINGERPRINT_MISMATCH",
                "evidence response does not match the committed bundle",
            )
        return existing

    recovered = recover_evidence_result(
        workspace,
        snapshot_job,
        media_manifest,
        evidence_transcript_manifest,
        evidence_segments,
        commit=False,
    )
    if (
        recovered is not None
        and recovered.evidence_manifest.response_fingerprint != response_fingerprint
    ):
        raise integrity_error(
            "EVIDENCE_FINGERPRINT_MISMATCH",
            "recoverable evidence bundle uses a different response",
        )

    attempt = None
    operation_failed = False
    try:
        staged = recovered
        if staged is None:
            attempt = create_evidence_attempt_directory(workspace)
            staged = stage_evidence(
                workspace,
                snapshot_job,
                media_manifest,
                evidence_transcript_manifest,
                evidence_segments,
                request,
                response,
                attempt,
            )

        # Re-verify every canonical upstream bundle after expensive response validation and
        # private staging. This keeps stale attempts from publishing any canonical output.
        current_raw_artifact = verify_acquired_bundle(workspace, snapshot_job)
        current_media_result = load_normalization_result(
            workspace, snapshot_job, current_raw_artifact
        )
        current_transcription = load_transcription_result(
            workspace, snapshot_job, current_media_result.media_manifest
        )
        current_evidence_transcript, current_evidence_segments = _evidence_transcript_inputs(
            workspace, snapshot_job, current_media_result.media_manifest, current_transcription
        )
        current_request = load_evidence_request(
            workspace,
            snapshot_job,
            current_media_result.media_manifest,
            current_evidence_transcript,
            current_evidence_segments,
        )
        if (
            current_raw_artifact != raw_artifact
            or current_media_result != media_result
            or current_transcription != transcription
            or current_evidence_transcript != evidence_transcript_manifest
            or current_evidence_segments != evidence_segments
            or current_request != request
        ):
            raise state_error(
                "EVIDENCE_SNAPSHOT_STALE",
                "canonical evidence inputs changed while importing the response",
            )
        with exclusive_append_lock(workspace.transitions_jsonl):
            workspace, current_job, transitions, state = _load_context(path)
            current_authorization = _authorization(workspace)
            if (
                state is not _evidence_predecessor(current_job)
                or transitions[-1].sequence != snapshot_sequence
                or current_job != snapshot_job
                or current_authorization != snapshot_authorization
            ):
                raise state_error(
                    "EVIDENCE_SNAPSHOT_STALE",
                    "job or evidence request changed while importing the response",
                )
            # The final publication boundary performs complete local semantic and hash
            # verification while the transition log is locked. Analyzer execution and
            # response staging stay outside this short canonical mutation boundary.
            locked_raw_artifact = verify_acquired_bundle(workspace, current_job)
            locked_media_result = load_normalization_result(
                workspace, current_job, locked_raw_artifact
            )
            locked_transcription = load_transcription_result(
                workspace, current_job, locked_media_result.media_manifest
            )
            locked_evidence_transcript, locked_evidence_segments = _evidence_transcript_inputs(
                workspace, current_job, locked_media_result.media_manifest, locked_transcription
            )
            locked_request = load_evidence_request(
                workspace,
                current_job,
                locked_media_result.media_manifest,
                locked_evidence_transcript,
                locked_evidence_segments,
            )
            if (
                locked_raw_artifact != raw_artifact
                or locked_media_result != media_result
                or locked_transcription != transcription
                or locked_evidence_transcript != evidence_transcript_manifest
                or locked_evidence_segments != evidence_segments
                or locked_request != request
            ):
                raise state_error(
                    "EVIDENCE_SNAPSHOT_STALE",
                    "canonical evidence inputs changed before publication",
                )
            result = commit_staged_evidence(
                workspace,
                current_job,
                locked_media_result.media_manifest,
                locked_evidence_transcript,
                locked_evidence_segments,
                locked_request,
                response,
                staged,
                attempt=attempt,
            )
            _append_transition_locked(
                workspace,
                current_job,
                from_state=_evidence_predecessor(current_job),
                to_state=JobState.EVIDENCE_READY,
                trigger="evidence.import",
            )
            return result
    except Exception:
        operation_failed = True
        raise
    finally:
        if attempt is not None:
            try:
                cleanup_evidence_attempt(workspace, attempt)
            except VepError:
                if not operation_failed:
                    raise


def _load_fixed_evidence(workspace: JobWorkspace, job: JobSpec):
    raw_artifact = verify_acquired_bundle(workspace, job)
    media = load_normalization_result(workspace, job, raw_artifact).media_manifest
    transcription = load_transcription_result(workspace, job, media)
    evidence_transcript, evidence_segments = _evidence_transcript_inputs(
        workspace, job, media, transcription
    )
    evidence = load_evidence_result(
        workspace,
        job,
        media,
        evidence_transcript,
        evidence_segments,
    )
    request = load_evidence_request(
        workspace,
        job,
        media,
        evidence_transcript,
        evidence_segments,
    )
    return build_fixed_evidence_bundle(
        job,
        request,
        evidence.evidence_manifest,
        evidence.evidence_records,
        evidence.ambiguity_records,
    )


def _latest_draft_version(workspace: JobWorkspace) -> str | None:
    found = [
        version
        for version in ("v001", "v002", "v003")
        if workspace.safe_path(f"manifests/authoring/draft-{version}.json").exists()
    ]
    if not found:
        return None
    expected = [f"v{index:03d}" for index in range(1, len(found) + 1)]
    if found != expected:
        raise integrity_error("DRAFT_HISTORY_INVALID", "draft history is not contiguous")
    return found[-1]


def _load_authoring_history(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed,
) -> tuple[tuple[DraftResult, AuditResult | None], ...]:
    latest = _latest_draft_version(workspace)
    if latest is None:
        return ()
    count = int(latest[1:])
    history: list[tuple[DraftResult, AuditResult | None]] = []
    for index in range(1, count + 1):
        version = f"v{index:03d}"
        if index == 1:
            draft = load_draft(workspace, job, version, fixed)
            request = load_authoring_request(workspace, job, fixed)
            if (
                draft.manifest.request_fingerprint != request.request_fingerprint
                or draft.agent_run.skill != request.skill
            ):
                raise integrity_error(
                    "DRAFT_HISTORY_INVALID", "initial draft request lineage is invalid"
                )
        else:
            predecessor, predecessor_audit = history[-1]
            if predecessor_audit is None:
                raise integrity_error(
                    "DRAFT_HISTORY_INVALID", "revision predecessor audit is absent"
                )
            request = load_revision_request(
                workspace,
                job,
                fixed,
                predecessor,
                predecessor_audit,
            )
            revision = load_revision(
                workspace,
                job,
                version,
                fixed,
                predecessor,
                predecessor_audit,
                request,
                tuple(history),
            )
            draft = revision.draft
        audit_path = workspace.safe_path(f"manifests/authoring/audit-{version}.json")
        pending_history = (*history, (draft, None))
        audit = (
            load_audit(workspace, job, draft, fixed, pending_history)
            if audit_path.exists()
            else None
        )
        history.append((draft, audit))
    return tuple(history)


def _load_authoring_state_integrity(
    workspace: JobWorkspace,
    job: JobSpec,
    state: JobState,
) -> tuple[
    FixedEvidenceBundle,
    tuple[tuple[DraftResult, AuditResult | None], ...],
    HumanReviewResult | None,
]:
    fixed = _load_fixed_evidence(workspace, job)
    history = _load_authoring_history(workspace, job, fixed)
    if not history:
        raise integrity_error("DRAFT_HISTORY_INVALID", "draft history is absent")
    draft, audit = history[-1]
    if state is JobState.DRAFTED:
        return fixed, history, None
    if audit is None:
        raise integrity_error("AUDIT_BUNDLE_INVALID", "latest audit is absent")
    expected_disposition = {
        JobState.REVISION_REQUIRED: AuditDisposition.REVISION_REQUIRED,
        JobState.HUMAN_REVIEW_READY: AuditDisposition.HUMAN_REVIEW_READY,
        JobState.BLOCKED: AuditDisposition.BLOCKED,
        JobState.HUMAN_APPROVED: AuditDisposition.HUMAN_REVIEW_READY,
        JobState.EXPORTED: AuditDisposition.HUMAN_REVIEW_READY,
    }.get(state)
    if expected_disposition is not None and audit.manifest.disposition is not expected_disposition:
        raise integrity_error("AUDIT_BUNDLE_INVALID", "audit disposition does not match state")
    human_review = None
    if state in {
        JobState.HUMAN_REVIEW_READY,
        JobState.HUMAN_APPROVED,
        JobState.EXPORTED,
    }:
        human_review = load_human_review(workspace, job, fixed, draft, audit, history)
    return fixed, history, human_review


def _approval_upstream(
    workspace: JobWorkspace,
    job: JobSpec,
    state: JobState,
    *,
    stale_code: bool,
) -> tuple[FixedEvidenceBundle, DraftResult, AuditResult, HumanReviewResult]:
    try:
        fixed, history, human_review = _load_authoring_state_integrity(workspace, job, state)
    except VepError as exc:
        if not stale_code:
            raise
        raise integrity_error(
            "APPROVAL_STALE", "approval no longer matches verified upstream state"
        ) from exc
    draft, audit = history[-1]
    if audit is None or human_review is None:
        raise integrity_error("APPROVAL_STALE", "approval upstream is incomplete")
    return fixed, draft, audit, human_review


def approve_job(
    path: Path,
    *,
    approval_target_sha256: str,
    reviewer: str,
    basis: str,
    confirm: bool,
    unresolved_notes: list[str] | None = None,
) -> ApprovalResult:
    if not confirm:
        raise state_error(
            "APPROVAL_CONFIRMATION_REQUIRED",
            "human approval requires explicit --confirm",
        )
    if not reviewer.strip():
        raise state_error("APPROVAL_REVIEWER_REQUIRED", "reviewer identity must be explicit")
    if not basis.strip():
        raise state_error("APPROVAL_BASIS_REQUIRED", "approval basis must be explicit")
    basis = basis.strip()
    notes = list(unresolved_notes or [])
    if any(not note.strip() for note in notes):
        raise state_error("APPROVAL_NOTE_INVALID", "unresolved notes cannot be blank")
    notes = sorted({note.strip() for note in notes})
    reviewer_identity = ReviewerIdentity(identity=reviewer)

    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in {
            JobState.HUMAN_REVIEW_READY,
            JobState.HUMAN_APPROVED,
            JobState.EXPORTED,
        }:
            raise state_error(
                "STATE_GATE_FAILED",
                "job must be HUMAN_REVIEW_READY before approval",
            )
        try:
            authorization = _authorization(workspace)
            _require_current_authorization(job, transitions, authorization)
        except VepError as exc:
            if state in {JobState.HUMAN_APPROVED, JobState.EXPORTED} or (
                workspace.approval_records_jsonl.exists()
            ):
                raise integrity_error(
                    "APPROVAL_STALE", "approval no longer matches the current job"
                ) from exc
            raise
        fixed, draft, audit, human_review = _approval_upstream(
            workspace,
            job,
            state,
            stale_code=state in {JobState.HUMAN_APPROVED, JobState.EXPORTED}
            or workspace.approval_records_jsonl.exists(),
        )
        del fixed
        if approval_target_sha256 != human_review.manifest.approval_target_sha256:
            raise state_error(
                "APPROVAL_TARGET_MISMATCH",
                "approval target does not match the current human-review bundle",
            )
        ensure_approval_confirmation_resolved(workspace, draft, human_review)
        result = approve_or_recover(
            workspace,
            job,
            draft,
            audit,
            human_review,
            reviewer=reviewer_identity,
            basis=basis,
            unresolved_notes=notes,
            approved_at=datetime.now(UTC),
        )
        if state is JobState.HUMAN_REVIEW_READY:
            _append_transition_locked(
                workspace,
                job,
                from_state=JobState.HUMAN_REVIEW_READY,
                to_state=JobState.HUMAN_APPROVED,
                trigger="job.approve",
            )
        return result


def export_job(path: Path) -> ExportResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in {JobState.HUMAN_APPROVED, JobState.EXPORTED}:
            raise state_error("STATE_GATE_FAILED", "job must be HUMAN_APPROVED before export")
        if job.output.format != "markdown":
            raise configuration_error(
                "EXPORT_FORMAT_NOT_IMPLEMENTED",
                "only the byte-exact markdown export format is implemented",
            )
        try:
            authorization = _authorization(workspace)
            _require_current_authorization(job, transitions, authorization)
            fixed, draft, audit, human_review = _approval_upstream(
                workspace, job, state, stale_code=True
            )
            approval = load_approval(workspace, job, draft, audit, human_review)
        except VepError as exc:
            raise integrity_error(
                "APPROVAL_STALE", "approval no longer matches verified upstream state"
            ) from exc
        ensure_approved_draft_marker_free(workspace, approval.record.approved_draft_artifact)
        if state is JobState.EXPORTED:
            return load_export(workspace, job, fixed, approval)
        result = export_or_recover(
            workspace,
            job,
            fixed,
            approval,
            created_at=datetime.now(UTC),
        )
        _append_transition_locked(
            workspace,
            job,
            from_state=JobState.HUMAN_APPROVED,
            to_state=JobState.EXPORTED,
            trigger="job.export",
        )
        return result


def _authoring_context_ids(
    history: tuple[tuple[DraftResult, AuditResult | None], ...],
) -> tuple[str, ...]:
    return tuple(sorted({draft.manifest.producer.context_id for draft, _audit in history}))


def _audit_forbidden_context_ids(
    history: tuple[tuple[DraftResult, AuditResult | None], ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *(_authoring_context_ids(history)),
                *(
                    audit.manifest.auditor.context_id
                    for _draft, audit in history[:-1]
                    if audit is not None
                ),
            }
        )
    )


def _all_agent_context_ids(
    history: tuple[tuple[DraftResult, AuditResult | None], ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *(_authoring_context_ids(history)),
                *(
                    audit.manifest.auditor.context_id
                    for _draft, audit in history
                    if audit is not None
                ),
            }
        )
    )


def prepare_authoring_job(path: Path, skill_id: str) -> AuthoringPrepareResult:
    workspace = JobWorkspace.open(path)
    with (
        exclusive_file_lock(
            workspace.semantic_lock_path,
            busy_code="SEMANTIC_STAGE_BUSY",
            busy_message="authoring, audit, or revision is already running",
            failure_code="SEMANTIC_STAGE_LOCK_FAILED",
            failure_message="semantic stage could not be locked",
        ),
        exclusive_append_lock(workspace.transitions_jsonl),
    ):
        workspace, job, transitions, state = _load_context(path)
        if state not in {
            JobState.EVIDENCE_READY,
            JobState.DRAFTED,
            JobState.AUDITED,
            JobState.REVISION_REQUIRED,
            JobState.HUMAN_REVIEW_READY,
            JobState.BLOCKED,
        }:
            raise state_error("STATE_GATE_FAILED", "job must be EVIDENCE_READY before authoring")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        fixed = _load_fixed_evidence(workspace, job)
        return prepare_authoring_request(workspace, job, fixed, skill_id)


def import_authoring_job(
    path: Path,
    response_path: Path,
    *,
    _allow_fixture: bool = False,
) -> DraftResult:
    workspace = JobWorkspace.open(path)
    with exclusive_file_lock(
        workspace.semantic_lock_path,
        busy_code="SEMANTIC_STAGE_BUSY",
        busy_message="authoring, audit, or revision is already running",
        failure_code="SEMANTIC_STAGE_LOCK_FAILED",
        failure_message="semantic stage could not be locked",
    ):
        cleanup_stale_authoring_attempts(workspace)
        return _import_authoring_job_locked(path, response_path, allow_fixture=_allow_fixture)


def _import_authoring_fixture(path: Path, response_path: Path) -> DraftResult:
    return import_authoring_job(path, response_path, _allow_fixture=True)


def _import_authoring_job_locked(
    path: Path,
    response_path: Path,
    *,
    allow_fixture: bool,
) -> DraftResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in {
            JobState.EVIDENCE_READY,
            JobState.DRAFTED,
            JobState.AUDITED,
            JobState.REVISION_REQUIRED,
            JobState.HUMAN_REVIEW_READY,
            JobState.BLOCKED,
        }:
            raise state_error("STATE_GATE_FAILED", "job must be EVIDENCE_READY before authoring")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        snapshot_sequence = transitions[-1].sequence
        snapshot_job = job
        snapshot_authorization = authorization
        fixed = _load_fixed_evidence(workspace, job)
        request = load_authoring_request(workspace, job, fixed)
        existing = (
            load_draft(workspace, job, "v001", fixed)
            if state is not JobState.EVIDENCE_READY
            else None
        )
    if existing is not None:
        response = read_authoring_response(
            workspace,
            response_path,
            request,
            allow_fixture=allow_fixture,
            enforce_current_skill=False,
        )
        if response.author.external_api_used and not snapshot_authorization.allow_external_api:
            raise state_error(
                "EXTERNAL_API_NOT_AUTHORIZED",
                "authoring response used an external API without authorization",
            )
        if (
            existing.manifest.response_fingerprint != fingerprint(response)
            or existing.response != response
        ):
            raise integrity_error("AUTHORING_FINGERPRINT_MISMATCH", "draft uses another response")
        return existing
    response = read_authoring_response(
        workspace,
        response_path,
        request,
        allow_fixture=allow_fixture,
    )
    if response.author.external_api_used and not snapshot_authorization.allow_external_api:
        raise state_error(
            "EXTERNAL_API_NOT_AUTHORIZED",
            "authoring response used an external API without authorization",
        )
    response_fingerprint = fingerprint(response)

    attempt = create_authoring_attempt_directory(workspace)
    operation_failed = False
    try:
        recovered_orphan = False
        if workspace.safe_path("manifests/authoring/draft-v001.json").exists():
            staged = load_draft(workspace, job, "v001", fixed)
            if staged.manifest.response_fingerprint != response_fingerprint:
                raise integrity_error(
                    "AUTHORING_FINGERPRINT_MISMATCH", "recoverable draft uses another response"
                )
        else:
            staged = recover_initial_draft(workspace, job, request, response)
            if staged is None:
                staged = stage_initial_draft(
                    workspace,
                    job,
                    request,
                    response,
                    attempt,
                )
            else:
                recovered_orphan = True
        with exclusive_append_lock(workspace.transitions_jsonl):
            workspace, current_job, transitions, current_state = _load_context(path)
            current_authorization = _authorization(workspace)
            if (
                current_state is not JobState.EVIDENCE_READY
                or transitions[-1].sequence != snapshot_sequence
                or current_job != snapshot_job
                or current_authorization != snapshot_authorization
            ):
                raise state_error("AUTHORING_SNAPSHOT_STALE", "job changed during authoring")
            current_fixed = _load_fixed_evidence(workspace, current_job)
            current_request = load_authoring_request(workspace, current_job, current_fixed)
            if current_fixed != fixed or current_request != request:
                raise state_error("AUTHORING_SNAPSHOT_STALE", "evidence changed during authoring")
            if not staged.reused:
                staged = commit_draft(
                    workspace,
                    staged,
                    attempt=None if recovered_orphan else attempt,
                )
            else:
                staged = load_draft(workspace, current_job, "v001", current_fixed)
            _append_transition_locked(
                workspace,
                current_job,
                from_state=JobState.EVIDENCE_READY,
                to_state=JobState.DRAFTED,
                trigger="author.import",
            )
            return staged
    except Exception:
        operation_failed = True
        raise
    finally:
        try:
            cleanup_authoring_attempt(workspace, attempt)
        except VepError:
            if not operation_failed:
                raise


def prepare_audit_job(path: Path, skill_id: str) -> AuditPrepareResult:
    workspace = JobWorkspace.open(path)
    with (
        exclusive_file_lock(
            workspace.semantic_lock_path,
            busy_code="SEMANTIC_STAGE_BUSY",
            busy_message="authoring, audit, or revision is already running",
            failure_code="SEMANTIC_STAGE_LOCK_FAILED",
            failure_message="semantic stage could not be locked",
        ),
        exclusive_append_lock(workspace.transitions_jsonl),
    ):
        workspace, job, transitions, state = _load_context(path)
        if state not in {
            JobState.DRAFTED,
            JobState.AUDITED,
            JobState.REVISION_REQUIRED,
            JobState.HUMAN_REVIEW_READY,
            JobState.BLOCKED,
        }:
            raise state_error("STATE_GATE_FAILED", "job must be DRAFTED before audit")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        fixed = _load_fixed_evidence(workspace, job)
        history = _load_authoring_history(workspace, job, fixed)
        if not history:
            raise integrity_error("DRAFT_HISTORY_INVALID", "draft history is absent")
        draft = history[-1][0]
        return prepare_audit_request(workspace, job, fixed, draft, skill_id)


def import_audit_job(
    path: Path,
    response_path: Path,
    *,
    _allow_fixture: bool = False,
) -> AuditResult:
    workspace = JobWorkspace.open(path)
    with exclusive_file_lock(
        workspace.semantic_lock_path,
        busy_code="SEMANTIC_STAGE_BUSY",
        busy_message="authoring, audit, or revision is already running",
        failure_code="SEMANTIC_STAGE_LOCK_FAILED",
        failure_message="semantic stage could not be locked",
    ):
        cleanup_stale_authoring_attempts(workspace)
        return _import_audit_job_locked(path, response_path, allow_fixture=_allow_fixture)


def _import_audit_fixture(path: Path, response_path: Path) -> AuditResult:
    return import_audit_job(path, response_path, _allow_fixture=True)


def _audit_with_human_review(audit: AuditResult, human_review) -> AuditResult:
    return AuditResult(
        manifest=audit.manifest,
        stage_manifest=audit.stage_manifest,
        response=audit.response,
        audit_claims=audit.audit_claims,
        claim_comparisons=audit.claim_comparisons,
        findings=audit.findings,
        agent_run=audit.agent_run,
        human_review=human_review.manifest if human_review is not None else None,
        reused=audit.reused,
    )


def _import_audit_job_locked(
    path: Path,
    response_path: Path,
    *,
    allow_fixture: bool,
) -> AuditResult:
    workspace = JobWorkspace.open(path)
    allowed_states = {
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.HUMAN_REVIEW_READY,
        JobState.BLOCKED,
    }
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in allowed_states:
            raise state_error("STATE_GATE_FAILED", "job must be DRAFTED before audit")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        snapshot_sequence = transitions[-1].sequence
        snapshot_state = state
        snapshot_job = job
        snapshot_authorization = authorization
        fixed = _load_fixed_evidence(workspace, job)
        history = _load_authoring_history(workspace, job, fixed)
        if not history:
            raise integrity_error("DRAFT_HISTORY_INVALID", "draft history is absent")
        draft = history[-1][0]
        request = load_audit_request(workspace, job, fixed, draft)
        context_ids = _audit_forbidden_context_ids(history)
        completed_existing = None
        if state in {
            JobState.REVISION_REQUIRED,
            JobState.HUMAN_REVIEW_READY,
            JobState.BLOCKED,
        }:
            pending_history = (*history[:-1], (draft, None))
            completed_existing = load_audit(workspace, job, draft, fixed, pending_history)
    response = read_audit_response(
        workspace,
        response_path,
        request,
        forbidden_context_ids=context_ids,
        allow_fixture=allow_fixture,
        enforce_current_skill=completed_existing is None,
    )
    if response.auditor.external_api_used and not snapshot_authorization.allow_external_api:
        raise state_error(
            "EXTERNAL_API_NOT_AUTHORIZED",
            "audit response used an external API without authorization",
        )
    response_fingerprint = fingerprint(response)
    audit_path = workspace.safe_path(
        f"manifests/authoring/audit-{draft.manifest.draft_version}.json"
    )
    if completed_existing is not None:
        if (
            completed_existing.manifest.response_fingerprint != response_fingerprint
            or completed_existing.response != response
        ):
            raise integrity_error("AUDIT_FINGERPRINT_MISMATCH", "audit uses another response")
        human = None
        if snapshot_state is JobState.HUMAN_REVIEW_READY:
            human = load_human_review(workspace, job, fixed, draft, completed_existing, history)
        return _audit_with_human_review(completed_existing, human)

    attempt = create_authoring_attempt_directory(workspace)
    operation_failed = False
    try:
        recovered_orphan = False
        if audit_path.exists():
            pending_history = (*history[:-1], (draft, None))
            staged = load_audit(workspace, job, draft, fixed, pending_history)
            if staged.manifest.response_fingerprint != response_fingerprint:
                raise integrity_error(
                    "AUDIT_FINGERPRINT_MISMATCH", "recoverable audit uses another response"
                )
        else:
            staged = recover_audit(
                workspace,
                job,
                request,
                response,
                draft,
                (*history[:-1], (draft, None)),
            )
            if staged is None:
                staged = stage_audit(
                    workspace,
                    job,
                    request,
                    response,
                    draft,
                    (*history[:-1], (draft, None)),
                    attempt,
                )
            else:
                recovered_orphan = True
        staged_history = (*history[:-1], (draft, staged))
        human_staged = None
        human_recovered_orphan = False
        if staged.manifest.disposition.value == "human_review_ready":
            if workspace.human_review_manifest_json.exists():
                human_staged = load_human_review(
                    workspace,
                    job,
                    fixed,
                    draft,
                    staged,
                    staged_history,
                )
            else:
                human_staged = recover_human_review(
                    workspace,
                    job,
                    fixed,
                    draft,
                    staged,
                    staged_history,
                )
                if human_staged is None:
                    human_staged = stage_human_review(
                        workspace,
                        job,
                        fixed,
                        draft,
                        staged,
                        staged_history,
                        attempt,
                    )
                else:
                    human_recovered_orphan = True
        with exclusive_append_lock(workspace.transitions_jsonl):
            workspace, current_job, transitions, current_state = _load_context(path)
            current_authorization = _authorization(workspace)
            if (
                current_state is not snapshot_state
                or transitions[-1].sequence != snapshot_sequence
                or current_job != snapshot_job
                or current_authorization != snapshot_authorization
            ):
                raise state_error("AUDIT_SNAPSHOT_STALE", "job changed during audit")
            current_fixed = _load_fixed_evidence(workspace, current_job)
            current_history = _load_authoring_history(workspace, current_job, current_fixed)
            current_draft = current_history[-1][0]
            current_request = load_audit_request(
                workspace, current_job, current_fixed, current_draft
            )
            if (
                current_fixed != fixed
                or current_draft.manifest != draft.manifest
                or current_request != request
                or _audit_forbidden_context_ids(current_history) != context_ids
            ):
                raise state_error("AUDIT_SNAPSHOT_STALE", "audit inputs changed")
            if not staged.reused:
                staged = commit_audit(
                    workspace,
                    staged,
                    attempt=None if recovered_orphan else attempt,
                )
            else:
                pending_history = (*current_history[:-1], (current_draft, None))
                staged = load_audit(
                    workspace,
                    current_job,
                    current_draft,
                    current_fixed,
                    pending_history,
                )
            if current_state is JobState.DRAFTED:
                _append_transition_locked(
                    workspace,
                    current_job,
                    from_state=JobState.DRAFTED,
                    to_state=JobState.AUDITED,
                    trigger="audit.import",
                )
                current_state = JobState.AUDITED
            disposition = staged.manifest.disposition
            if disposition.value == "human_review_ready":
                if human_staged is None:
                    raise integrity_error(
                        "HUMAN_REVIEW_BUNDLE_INVALID", "human review staging is absent"
                    )
                if not human_staged.reused:
                    human_staged = commit_human_review(
                        workspace,
                        human_staged,
                        attempt=None if human_recovered_orphan else attempt,
                    )
                else:
                    human_staged = load_human_review(
                        workspace,
                        current_job,
                        current_fixed,
                        current_draft,
                        staged,
                        (*current_history[:-1], (current_draft, staged)),
                    )
                _append_transition_locked(
                    workspace,
                    current_job,
                    from_state=JobState.AUDITED,
                    to_state=JobState.HUMAN_REVIEW_READY,
                    trigger="audit.decision",
                )
                return _audit_with_human_review(staged, human_staged)
            target = (
                JobState.REVISION_REQUIRED
                if disposition.value == "revision_required"
                else JobState.BLOCKED
            )
            _append_transition_locked(
                workspace,
                current_job,
                from_state=JobState.AUDITED,
                to_state=target,
                trigger="audit.decision",
            )
            return staged
    except Exception:
        operation_failed = True
        raise
    finally:
        try:
            cleanup_authoring_attempt(workspace, attempt)
        except VepError:
            if not operation_failed:
                raise


def prepare_revision_job(path: Path, skill_id: str) -> RevisionPrepareResult:
    workspace = JobWorkspace.open(path)
    with (
        exclusive_file_lock(
            workspace.semantic_lock_path,
            busy_code="SEMANTIC_STAGE_BUSY",
            busy_message="authoring, audit, or revision is already running",
            failure_code="SEMANTIC_STAGE_LOCK_FAILED",
            failure_message="semantic stage could not be locked",
        ),
        exclusive_append_lock(workspace.transitions_jsonl),
    ):
        workspace, job, transitions, state = _load_context(path)
        if state is not JobState.REVISION_REQUIRED:
            raise state_error("STATE_GATE_FAILED", "job must be REVISION_REQUIRED")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        fixed = _load_fixed_evidence(workspace, job)
        history = _load_authoring_history(workspace, job, fixed)
        if not history:
            raise integrity_error("DRAFT_HISTORY_INVALID", "draft history is absent")
        target = history[-1]
        if target[1] is None and target[0].manifest.revision_count > 0 and len(history) > 1:
            target = history[-2]
        draft, audit = target
        if audit is None:
            raise integrity_error("AUDIT_BUNDLE_INVALID", "revision audit is absent")
        return prepare_revision_request(workspace, job, fixed, draft, audit, skill_id)


def import_revision_job(
    path: Path,
    response_path: Path,
    *,
    _allow_fixture: bool = False,
) -> RevisionResult:
    workspace = JobWorkspace.open(path)
    with exclusive_file_lock(
        workspace.semantic_lock_path,
        busy_code="SEMANTIC_STAGE_BUSY",
        busy_message="authoring, audit, or revision is already running",
        failure_code="SEMANTIC_STAGE_LOCK_FAILED",
        failure_message="semantic stage could not be locked",
    ):
        cleanup_stale_authoring_attempts(workspace)
        return _import_revision_job_locked(path, response_path, allow_fixture=_allow_fixture)


def _import_revision_fixture(path: Path, response_path: Path) -> RevisionResult:
    return import_revision_job(path, response_path, _allow_fixture=True)


def _import_revision_job_locked(
    path: Path,
    response_path: Path,
    *,
    allow_fixture: bool,
) -> RevisionResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transitions, state = _load_context(path)
        if state not in {JobState.REVISION_REQUIRED, JobState.DRAFTED}:
            raise state_error("STATE_GATE_FAILED", "job must be REVISION_REQUIRED")
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        fixed = _load_fixed_evidence(workspace, job)
        history = _load_authoring_history(workspace, job, fixed)
        if not history:
            raise integrity_error("DRAFT_HISTORY_INVALID", "draft history is absent")
        published_revision = history[-1][1] is None and history[-1][0].manifest.revision_count > 0
        if state is JobState.DRAFTED or published_revision:
            latest = history[-1][0]
            if latest.manifest.revision_count == 0:
                raise state_error("STATE_GATE_FAILED", "no completed revision exists")
            previous_draft, previous_audit = history[-2]
            if previous_audit is None:
                raise integrity_error("AUDIT_BUNDLE_INVALID", "revision audit is absent")
            request = load_revision_request(workspace, job, fixed, previous_draft, previous_audit)
            existing = load_revision(
                workspace,
                job,
                latest.manifest.draft_version,
                fixed,
                previous_draft,
                previous_audit,
                request,
                history[:-1],
            )
            response = read_revision_response(
                workspace,
                response_path,
                request,
                forbidden_context_ids=_all_agent_context_ids(history[:-1]),
                allow_fixture=allow_fixture,
                enforce_current_skill=False,
            )
            if (
                existing.manifest.response_fingerprint != fingerprint(response)
                or existing.draft.response != response
            ):
                raise integrity_error(
                    "REVISION_FINGERPRINT_MISMATCH", "revision uses another response"
                )
            if response.reviser.external_api_used and not authorization.allow_external_api:
                raise state_error(
                    "EXTERNAL_API_NOT_AUTHORIZED",
                    "revision response used an external API without authorization",
                )
            if state is JobState.REVISION_REQUIRED:
                _append_transition_locked(
                    workspace,
                    job,
                    from_state=JobState.REVISION_REQUIRED,
                    to_state=JobState.DRAFTED,
                    trigger="revision.import",
                )
            return existing
        draft, audit = history[-1]
        if audit is None:
            raise integrity_error("AUDIT_BUNDLE_INVALID", "revision audit is absent")
        request = load_revision_request(workspace, job, fixed, draft, audit)
        forbidden_contexts = _all_agent_context_ids(history)
        snapshot_sequence = transitions[-1].sequence
        snapshot_job = job
        snapshot_authorization = authorization
    response = read_revision_response(
        workspace,
        response_path,
        request,
        forbidden_context_ids=forbidden_contexts,
        allow_fixture=allow_fixture,
    )
    if response.reviser.external_api_used and not snapshot_authorization.allow_external_api:
        raise state_error(
            "EXTERNAL_API_NOT_AUTHORIZED",
            "revision response used an external API without authorization",
        )
    response_fingerprint = fingerprint(response)
    next_version = request.next_draft_version
    attempt = create_authoring_attempt_directory(workspace)
    operation_failed = False
    try:
        revision_path = workspace.safe_path(f"manifests/authoring/revision-{next_version}.json")
        draft_path = workspace.safe_path(f"manifests/authoring/draft-{next_version}.json")
        recovered_orphan = False
        if revision_path.exists() and draft_path.exists():
            staged = load_revision(
                workspace,
                job,
                next_version,
                fixed,
                draft,
                audit,
                request,
                history,
            )
            if staged.manifest.response_fingerprint != response_fingerprint:
                raise integrity_error(
                    "REVISION_FINGERPRINT_MISMATCH",
                    "recoverable revision uses another response",
                )
        else:
            staged = recover_revision(
                workspace,
                job,
                request,
                response,
                draft,
                audit,
                history,
            )
            if staged is None:
                if revision_path.exists() or draft_path.exists():
                    raise integrity_error(
                        "REVISION_ORPHAN_INVALID",
                        "revision manifest or draft exists without a recoverable agent run",
                    )
                staged = stage_revision(
                    workspace,
                    job,
                    request,
                    response,
                    draft,
                    audit,
                    history,
                    attempt,
                )
            else:
                recovered_orphan = True
        with exclusive_append_lock(workspace.transitions_jsonl):
            workspace, current_job, transitions, current_state = _load_context(path)
            current_authorization = _authorization(workspace)
            if (
                current_state is not JobState.REVISION_REQUIRED
                or transitions[-1].sequence != snapshot_sequence
                or current_job != snapshot_job
                or current_authorization != snapshot_authorization
            ):
                raise state_error("REVISION_SNAPSHOT_STALE", "job changed during revision")
            current_fixed = _load_fixed_evidence(workspace, current_job)
            current_history = _load_authoring_history(workspace, current_job, current_fixed)
            current_draft, current_audit = current_history[-1]
            if current_audit is None:
                raise integrity_error("AUDIT_BUNDLE_INVALID", "revision audit is absent")
            current_request = load_revision_request(
                workspace,
                current_job,
                current_fixed,
                current_draft,
                current_audit,
            )
            if (
                current_fixed != fixed
                or current_draft.manifest != draft.manifest
                or current_audit.manifest != audit.manifest
                or current_request != request
            ):
                raise state_error("REVISION_SNAPSHOT_STALE", "revision inputs changed")
            if not staged.reused:
                staged = commit_revision(
                    workspace,
                    staged,
                    attempt=None if recovered_orphan else attempt,
                )
            else:
                staged = load_revision(
                    workspace,
                    current_job,
                    next_version,
                    current_fixed,
                    current_draft,
                    current_audit,
                    current_request,
                    current_history,
                )
            _append_transition_locked(
                workspace,
                current_job,
                from_state=JobState.REVISION_REQUIRED,
                to_state=JobState.DRAFTED,
                trigger="revision.import",
            )
            return staged
    except Exception:
        operation_failed = True
        raise
    finally:
        try:
            cleanup_authoring_attempt(workspace, attempt)
        except VepError:
            if not operation_failed:
                raise


def _append_transition_locked(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    from_state: JobState,
    to_state: JobState,
    trigger: str,
) -> None:
    require_implemented_transition(from_state, to_state)
    fingerprint = job_fingerprint(job)
    records = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    state = validate_runtime_transition_records(records, job.job_id)
    validate_transition_fingerprints(records, fingerprint)
    if state is not from_state:
        raise state_error("STATE_GATE_FAILED", "stored state changed before transition")
    record = TransitionRecord(
        sequence=records[-1].sequence + 1,
        job_id=job.job_id,
        from_state=from_state,
        to_state=to_state,
        trigger=trigger,
        occurred_at=datetime.now(UTC),
        job_fingerprint=fingerprint,
    )
    append_jsonl(workspace.transitions_jsonl, record)


def _load_search_inputs(
    path: Path,
) -> tuple[
    JobWorkspace,
    JobSpec,
    tuple[TranscriptManifest, tuple[TranscriptSegment, ...]],
    EvidenceImportResult,
]:
    workspace, job, transitions, state = _load_context(path)
    if state not in {
        JobState.EVIDENCE_READY,
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.HUMAN_REVIEW_READY,
        JobState.BLOCKED,
    }:
        raise state_error("STATE_GATE_FAILED", "job must have verified evidence before search")
    authorization = _authorization(workspace)
    _require_current_authorization(job, transitions, authorization)
    raw_artifact = verify_acquired_bundle(workspace, job)
    media_manifest = load_normalization_result(workspace, job, raw_artifact).media_manifest
    transcription = load_transcription_result(workspace, job, media_manifest)
    evidence_transcript, evidence_segments = _evidence_transcript_inputs(
        workspace, job, media_manifest, transcription
    )
    evidence = load_evidence_result(
        workspace,
        job,
        media_manifest,
        evidence_transcript,
        evidence_segments,
    )
    return workspace, job, (evidence_transcript, evidence_segments), evidence


def build_search_index_job(path: Path) -> SearchIndexBuildResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transcript_inputs, evidence = _load_search_inputs(path)
    return build_index(
        workspace,
        job.job_id,
        *transcript_inputs,
        evidence.evidence_manifest,
        evidence.evidence_records,
    )


def search_job(path: Path, query: str, limit: int = 20) -> SearchResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transcript_inputs, evidence = _load_search_inputs(path)
    return search_index(
        workspace,
        job.job_id,
        *transcript_inputs,
        evidence.evidence_manifest,
        evidence.evidence_records,
        query,
        limit,
    )


def job_status(path: Path) -> tuple[dict[str, Any], int]:
    workspace, job, transitions, state = _load_context(path)
    current_fingerprint = job_fingerprint(job)
    authorization_status: dict[str, Any] = {
        "present": workspace.authorization_json.exists(),
        "stale": False,
    }
    exit_code = 0
    if state is not JobState.CREATED and not workspace.authorization_json.exists():
        authorization_status.update({"valid": False, "error_code": "AUTHORIZATION_MISSING"})
        exit_code = 3
    if workspace.authorization_json.exists():
        try:
            authorization = _authorization(workspace)
            _validate_authorization_binding(job, transitions, authorization)
            authorization_status.update(
                {
                    "valid": True,
                    "stale": authorization.job_fingerprint != current_fingerprint,
                    "allow_local_processing": authorization.allow_local_processing,
                    "allow_network_acquisition": authorization.allow_network_acquisition,
                    "allow_external_api": authorization.allow_external_api,
                }
            )
            if authorization_status["stale"]:
                authorization_status["error_code"] = "AUTHORIZATION_STALE"
                exit_code = 3
        except VepError as exc:
            authorization_status.update({"valid": False, "error_code": exc.code})
            exit_code = int(exc.exit_code)

    artifact_status: dict[str, Any] = {"status": "not-applicable"}
    authoring_states = {
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.HUMAN_REVIEW_READY,
        JobState.BLOCKED,
        JobState.HUMAN_APPROVED,
        JobState.EXPORTED,
    }
    downstream_acquired_states = {
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.TRANSCRIPT_REFINED,
        JobState.EVIDENCE_READY,
        *authoring_states,
    }
    downstream_normalized_states = {
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.TRANSCRIPT_REFINED,
        JobState.EVIDENCE_READY,
        *authoring_states,
    }
    if state in downstream_acquired_states:
        try:
            artifact = verify_acquired_bundle(workspace, job)
            artifact_status = {
                "status": "valid",
                "artifact_id": artifact.artifact_id,
                "content_sha256": artifact.content_sha256,
                "size_bytes": artifact.size_bytes,
            }
        except VepError as exc:
            artifact_status = {"status": "invalid", "error_code": exc.code}
            exit_code = int(exc.exit_code)
    normalization_status: dict[str, Any] = {"status": "not-applicable"}
    media_manifest = None
    if state in downstream_normalized_states and artifact_status.get("status") == "valid":
        try:
            raw_artifact = verify_acquired_bundle(workspace, job)
            normalization_result = load_normalization_result(workspace, job, raw_artifact)
            media_manifest = normalization_result.media_manifest
            normalization_status = {
                "status": "valid",
                "normalization_fingerprint": media_manifest.normalization_fingerprint,
                "frame_count": (
                    len(media_manifest.frame_index.records)
                    if media_manifest.frame_index is not None
                    else 0
                ),
            }
        except VepError as exc:
            normalization_status = {"status": "invalid", "error_code": exc.code}
            exit_code = int(exc.exit_code)
    transcription_status: dict[str, Any] = {"status": "not-applicable"}
    transcription = None
    if (
        state
        in {
            JobState.TRANSCRIBED,
            JobState.TRANSCRIPT_REFINED,
            JobState.EVIDENCE_READY,
            *authoring_states,
        }
        and normalization_status.get("status") == "valid"
        and media_manifest is not None
    ):
        try:
            transcription = load_transcription_result(workspace, job, media_manifest)
            transcription_status = {
                "status": "valid",
                "transcription_fingerprint": (
                    transcription.transcript_manifest.transcription_fingerprint
                ),
                "segment_count": len(transcription.segments),
            }
        except VepError as exc:
            transcription_status = {"status": "invalid", "error_code": exc.code}
            exit_code = int(exc.exit_code)
    refinement_status: dict[str, Any] = {"status": "not-applicable"}
    evidence_transcript = None
    evidence_segments = None
    if transcription is not None:
        evidence_transcript = transcription.transcript_manifest
        evidence_segments = transcription.segments
        if requires_transcript_refinement(job) and state in {
            JobState.TRANSCRIPT_REFINED,
            JobState.EVIDENCE_READY,
            *authoring_states,
        }:
            try:
                refinement = load_refinement_result(
                    workspace,
                    job,
                    media_manifest,
                    transcription.transcript_manifest,
                    transcription.segments,
                )
                evidence_transcript = evidence_transcript_projection(
                    transcription.transcript_manifest, refinement
                )
                evidence_segments = refinement.segments
                refinement_status = {
                    "status": "valid",
                    "refined_transcript_fingerprint": (
                        refinement.refinement_manifest.refined_transcript_fingerprint
                    ),
                    "edit_count": len(refinement.edits),
                    "ambiguity_count": len(refinement.ambiguities),
                }
            except VepError as exc:
                refinement_status = {"status": "invalid", "error_code": exc.code}
                exit_code = int(exc.exit_code)
    evidence_status: dict[str, Any] = {"status": "not-applicable"}
    if (
        state in {JobState.EVIDENCE_READY, *authoring_states}
        and transcription_status.get("status") == "valid"
        and refinement_status.get("status") != "invalid"
        and media_manifest is not None
        and transcription is not None
        and evidence_transcript is not None
        and evidence_segments is not None
    ):
        try:
            evidence = load_evidence_result(
                workspace,
                job,
                media_manifest,
                evidence_transcript,
                evidence_segments,
            )
            evidence_status = {
                "status": "valid",
                "evidence_bundle_fingerprint": (
                    evidence.evidence_manifest.evidence_bundle_fingerprint
                ),
                "evidence_count": len(evidence.evidence_records),
                "ambiguity_count": len(evidence.ambiguity_records),
                "index_rebuildable": True,
            }
        except VepError as exc:
            evidence_status = {"status": "invalid", "error_code": exc.code}
            exit_code = int(exc.exit_code)
    authoring_status: dict[str, Any] = {"status": "not-applicable"}
    fixed_evidence = None
    delivery_history = None
    delivery_human_review = None
    if state in authoring_states and evidence_status.get("status") == "valid":
        try:
            fixed_evidence, history, human_review = _load_authoring_state_integrity(
                workspace,
                job,
                state,
            )
            delivery_history = history
            delivery_human_review = human_review
            latest_draft, latest_audit = history[-1]
            authoring_status = {
                "status": "valid",
                "draft_version": latest_draft.manifest.draft_version,
                "draft_sha256": latest_draft.manifest.draft_artifact.content_sha256,
                "revision_count": latest_draft.manifest.revision_count,
                "draft_bundle_fingerprint": latest_draft.manifest.bundle_fingerprint,
                "audit_bundle_fingerprint": (
                    latest_audit.manifest.bundle_fingerprint if latest_audit is not None else None
                ),
                "audit_disposition": (
                    latest_audit.manifest.disposition.value if latest_audit is not None else None
                ),
                "human_review_bundle_fingerprint": (
                    human_review.manifest.bundle_fingerprint if human_review is not None else None
                ),
            }
        except VepError as exc:
            authoring_status = {"status": "invalid", "error_code": exc.code}
            exit_code = int(exc.exit_code)
    approval_status: dict[str, Any] = {"status": "not-applicable"}
    approval_result = None
    if state in {JobState.HUMAN_APPROVED, JobState.EXPORTED}:
        if (
            authoring_status.get("status") != "valid"
            or delivery_history is None
            or delivery_human_review is None
        ):
            approval_status = {"status": "invalid", "error_code": "APPROVAL_STALE"}
            exit_code = max(exit_code, 5)
        else:
            try:
                delivery_draft, delivery_audit = delivery_history[-1]
                if delivery_audit is None:
                    raise integrity_error("APPROVAL_STALE", "approval audit is absent")
                approval_result = load_approval(
                    workspace,
                    job,
                    delivery_draft,
                    delivery_audit,
                    delivery_human_review,
                )
                approval_status = {
                    "status": "valid",
                    "approval_id": approval_result.record.approval_id,
                    "approval_record_sha256": approval_result.record.record_sha256,
                    "approved_draft_version": approval_result.record.approved_draft_version,
                    "approval_target_sha256": approval_result.record.approval_target_sha256,
                    "reviewer_assertion_method": (approval_result.record.reviewer.assertion_method),
                    "reviewer_cryptographically_verified": False,
                }
            except VepError:
                approval_status = {"status": "invalid", "error_code": "APPROVAL_STALE"}
                exit_code = max(exit_code, 5)
    export_status: dict[str, Any] = {"status": "not-applicable"}
    if state is JobState.EXPORTED:
        if approval_result is None or fixed_evidence is None:
            export_status = {"status": "invalid", "error_code": "APPROVAL_STALE"}
            exit_code = max(exit_code, 5)
        else:
            try:
                export_result = load_export(workspace, job, fixed_evidence, approval_result)
                export_status = {
                    "status": "valid",
                    "format": export_result.manifest.format,
                    "byte_exact": export_result.manifest.byte_exact,
                    "export_fingerprint": export_result.manifest.export_fingerprint,
                    "final_sha256": export_result.manifest.final_artifact.content_sha256,
                    "final_size_bytes": export_result.manifest.final_artifact.size_bytes,
                }
            except VepError as exc:
                export_status = {"status": "invalid", "error_code": exc.code}
                exit_code = int(exc.exit_code)
    return (
        {
            "job_id": job.job_id,
            "stored_state": state.value,
            "job_fingerprint": current_fingerprint,
            "authorization": authorization_status,
            "artifact_integrity": artifact_status,
            "normalization_integrity": normalization_status,
            "transcription_integrity": transcription_status,
            "refinement_integrity": refinement_status,
            "evidence_integrity": evidence_status,
            "authoring_integrity": authoring_status,
            "approval_integrity": approval_status,
            "export_integrity": export_status,
        },
        exit_code,
    )
