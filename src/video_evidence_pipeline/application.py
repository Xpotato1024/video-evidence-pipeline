"""Use cases that orchestrate domain, storage, and adapters."""

from __future__ import annotations

import getpass
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
from video_evidence_pipeline.domain.authorization import AuthorizationRecord
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    SourceKind,
    SourceSpec,
    canonicalize_source_locator,
    job_fingerprint,
    job_to_yaml,
    load_job_yaml,
)
from video_evidence_pipeline.domain.media import NormalizationConfig
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transcription import TranscriptionConfig
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
    atomic_write_json,
    atomic_write_new_bytes,
    read_json_model,
)
from video_evidence_pipeline.storage.jsonl import append_jsonl, exclusive_append_lock, read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.locking import exclusive_file_lock
from video_evidence_pipeline.transcription.base import AsrBackend
from video_evidence_pipeline.transcription.pipeline import (
    TranscriptionResult,
    load_transcription_result,
    media_bundle_fingerprint,
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
    job = JobSpec(
        job_id=job_id,
        created_at=datetime.now(UTC),
        source=SourceSpec(kind=source_kind, locator=locator),
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


def validate_job(path: Path) -> dict[str, Any]:
    workspace, job, transitions, state = _load_context(path)
    if state is not JobState.CREATED:
        authorization = _authorization(workspace)
        _validate_authorization_binding(job, transitions, authorization)
        _require_current_authorization_fingerprint(job, authorization)
    acquired_states = {
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.EVIDENCE_READY,
    }
    normalized_states = {
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.EVIDENCE_READY,
    }
    if state in acquired_states:
        raw_artifact = verify_acquired_bundle(workspace, job)
        if state in normalized_states:
            media_manifest = load_normalization_result(workspace, job, raw_artifact).media_manifest
            if state in {JobState.TRANSCRIBED, JobState.EVIDENCE_READY}:
                transcription = load_transcription_result(workspace, job, media_manifest)
                if state is JobState.EVIDENCE_READY:
                    load_evidence_result(
                        workspace,
                        job,
                        media_manifest,
                        transcription.transcript_manifest,
                        transcription.segments,
                    )
    return {
        "valid": True,
        "job_id": job.job_id,
        "job_fingerprint": job_fingerprint(job),
        "state": state.value,
        "workspace": workspace.root.name,
    }


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
        if state not in {JobState.TRANSCRIBED, JobState.EVIDENCE_READY}:
            raise state_error(
                "STATE_GATE_FAILED", "job must be TRANSCRIBED before evidence preparation"
            )
        authorization = _authorization(workspace)
        _require_current_authorization(job, transitions, authorization)
        raw_artifact = verify_acquired_bundle(workspace, job)
        media_result = load_normalization_result(workspace, job, raw_artifact)
        media_manifest = media_result.media_manifest
        transcription = load_transcription_result(workspace, job, media_manifest)
        if state is JobState.EVIDENCE_READY:
            load_evidence_result(
                workspace,
                job,
                media_manifest,
                transcription.transcript_manifest,
                transcription.segments,
            )
        return prepare_evidence_request(
            workspace,
            job,
            media_manifest,
            transcription.transcript_manifest,
            transcription.segments,
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
        if state not in {JobState.TRANSCRIBED, JobState.EVIDENCE_READY}:
            raise state_error("STATE_GATE_FAILED", "job must be TRANSCRIBED before evidence import")
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
    request = load_evidence_request(
        workspace,
        snapshot_job,
        media_manifest,
        transcription.transcript_manifest,
        transcription.segments,
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
            transcription.transcript_manifest,
            transcription.segments,
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
        transcription.transcript_manifest,
        transcription.segments,
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
                transcription.transcript_manifest,
                transcription.segments,
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
        current_request = load_evidence_request(
            workspace,
            snapshot_job,
            current_media_result.media_manifest,
            current_transcription.transcript_manifest,
            current_transcription.segments,
        )
        if (
            current_raw_artifact != raw_artifact
            or current_media_result != media_result
            or current_transcription != transcription
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
                state is not JobState.TRANSCRIBED
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
            locked_request = load_evidence_request(
                workspace,
                current_job,
                locked_media_result.media_manifest,
                locked_transcription.transcript_manifest,
                locked_transcription.segments,
            )
            if (
                locked_raw_artifact != raw_artifact
                or locked_media_result != media_result
                or locked_transcription != transcription
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
                locked_transcription.transcript_manifest,
                locked_transcription.segments,
                locked_request,
                response,
                staged,
                attempt=attempt,
            )
            _append_transition_locked(
                workspace,
                current_job,
                from_state=JobState.TRANSCRIBED,
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
    TranscriptionResult,
    EvidenceImportResult,
]:
    workspace, job, transitions, state = _load_context(path)
    if state is not JobState.EVIDENCE_READY:
        raise state_error("STATE_GATE_FAILED", "job must be EVIDENCE_READY before search")
    authorization = _authorization(workspace)
    _require_current_authorization(job, transitions, authorization)
    raw_artifact = verify_acquired_bundle(workspace, job)
    media_manifest = load_normalization_result(workspace, job, raw_artifact).media_manifest
    transcription = load_transcription_result(workspace, job, media_manifest)
    evidence = load_evidence_result(
        workspace,
        job,
        media_manifest,
        transcription.transcript_manifest,
        transcription.segments,
    )
    return workspace, job, transcription, evidence


def build_search_index_job(path: Path) -> SearchIndexBuildResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transcription, evidence = _load_search_inputs(path)
    return build_index(
        workspace,
        job.job_id,
        transcription.transcript_manifest,
        transcription.segments,
        evidence.evidence_manifest,
        evidence.evidence_records,
    )


def search_job(path: Path, query: str, limit: int = 20) -> SearchResult:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        workspace, job, transcription, evidence = _load_search_inputs(path)
    return search_index(
        workspace,
        job.job_id,
        transcription.transcript_manifest,
        transcription.segments,
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
    downstream_acquired_states = {
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.EVIDENCE_READY,
    }
    downstream_normalized_states = {
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.EVIDENCE_READY,
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
                "frame_count": len(media_manifest.frame_index.records),
            }
        except VepError as exc:
            normalization_status = {"status": "invalid", "error_code": exc.code}
            exit_code = int(exc.exit_code)
    transcription_status: dict[str, Any] = {"status": "not-applicable"}
    transcription = None
    if (
        state in {JobState.TRANSCRIBED, JobState.EVIDENCE_READY}
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
    evidence_status: dict[str, Any] = {"status": "not-applicable"}
    if (
        state is JobState.EVIDENCE_READY
        and transcription_status.get("status") == "valid"
        and media_manifest is not None
        and transcription is not None
    ):
        try:
            evidence = load_evidence_result(
                workspace,
                job,
                media_manifest,
                transcription.transcript_manifest,
                transcription.segments,
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
    return (
        {
            "job_id": job.job_id,
            "stored_state": state.value,
            "job_fingerprint": current_fingerprint,
            "authorization": authorization_status,
            "artifact_integrity": artifact_status,
            "normalization_integrity": normalization_status,
            "transcription_integrity": transcription_status,
            "evidence_integrity": evidence_status,
        },
        exit_code,
    )
