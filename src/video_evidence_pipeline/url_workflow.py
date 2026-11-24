"""Resumable public workflow for authorized Panopto URL jobs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from video_evidence_pipeline.acquisition.bundle import verify_acquired_bundle
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    initialize_job,
    job_status,
    normalize_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.domain.identifiers import JOB_ID_PATTERN
from video_evidence_pipeline.domain.job import (
    JobSpec,
    PrimaryInputKind,
    SourceKind,
    load_job_yaml,
    primary_input_kind,
)
from video_evidence_pipeline.domain.locators import canonicalize_panopto_input
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transcription import TranscriptionConfig
from video_evidence_pipeline.errors import (
    configuration_error,
    integrity_error,
    state_error,
)
from video_evidence_pipeline.media.normalization import load_normalization_result
from video_evidence_pipeline.runtime_context import current_runtime, require_source_asr
from video_evidence_pipeline.storage.layout import JobWorkspace

_RESUMABLE_STATES = {
    JobState.AUTHORIZED,
    JobState.ACQUIRED,
    JobState.NORMALIZED,
    JobState.TRANSCRIBED,
}


class UrlWorkflowStage(StrEnum):
    ACQUIRED = "acquired"
    TRANSCRIBED = "transcribed"


@dataclass(frozen=True, slots=True)
class UrlWorkflowResult:
    job_id: str
    workspace_root: str
    state: str
    raw_media_path: str | None
    analysis_media_path: str | None
    speech_audio_path: str | None
    transcript_segments_path: str | None
    frame_index_path: str | None
    acquisition_reused: bool
    normalization_reused: bool
    transcription_reused: bool
    frame_count: int
    frames: list[dict[str, Any]]
    segment_count: int
    transcript_sources: list[str]
    asr_backend: dict[str, Any]


def _verified_status(path: Path) -> tuple[JobWorkspace, JobSpec, dict[str, Any], JobState]:
    workspace = JobWorkspace.open(path)
    job = load_job_yaml(workspace.job_yaml)
    if job.job_id != workspace.root.name:
        raise integrity_error("JOB_WORKSPACE_MISMATCH", "job ID does not match workspace name")
    validate_job(workspace.root)
    status, exit_code = job_status(workspace.root)
    if exit_code:
        error_code = next(
            (
                section.get("error_code")
                for name, section in status.items()
                if name.endswith("_integrity")
                and isinstance(section, dict)
                and section.get("error_code")
            ),
            status.get("authorization", {}).get("error_code"),
        )
        if exit_code == 3:
            raise state_error(
                error_code or "JOB_AUTHORIZATION_INVALID",
                "job authorization must be repaired before the workflow can continue",
            )
        raise integrity_error(
            error_code or "JOB_INTEGRITY_FAILED",
            "job artifacts must pass integrity checks before the workflow can continue",
        )
    try:
        state = JobState(status["stored_state"])
    except (KeyError, ValueError):
        raise integrity_error("JOB_STATE_INVALID", "job state is invalid") from None
    return workspace, job, status, state


def _frame_rows(media_manifest: Any) -> list[dict[str, Any]]:
    index = getattr(media_manifest, "frame_index", None)
    if index is None:
        return []
    return [
        {
            "frame_id": record.frame_id,
            "timestamp_ms": record.timestamp_ms,
            "artifact": record.artifact.relative_storage_path,
            "content_sha256": record.artifact.content_sha256,
            "reasons": [reason.value for reason in record.reasons],
        }
        for record in index.records
    ]


def _artifact_path(owner: Any, field: str) -> str | None:
    artifact = getattr(owner, field, None)
    if artifact is None:
        return None
    return artifact.relative_storage_path


def _source_for_start(value: str) -> str:
    try:
        return canonicalize_panopto_input(value)
    except ValueError:
        raise configuration_error("SOURCE_LOCATOR_INVALID", "Panopto source is invalid") from None


def url_workflow_workspace_path(repository_root: Path, job_id: str) -> Path:
    runtime = current_runtime()
    jobs_root = (
        runtime.data.jobs_root if runtime else repository_root.absolute() / ".local" / "jobs"
    )
    return jobs_root / job_id


def start_url_workflow(
    repository_root: Path,
    *,
    job_id: str,
    source_locator: str,
    authorization_basis: str,
    confirm_save: bool,
    task_type: str = "summary",
    language: str = "ja",
    output_format: str = "markdown",
    requirements: list[str] | None = None,
    purpose: str | None = None,
    audience: str | None = None,
    confirmer_identity: str | None = None,
    runtime_config: AcquisitionRuntimeConfig | None = None,
    preferred_languages: list[str] | None = None,
    minimum_caption_coverage: float = 0.8,
    asr_language: str | None = None,
    asr_profile: str = "whisper-large-v3",
    asr_device: str = "cuda",
    stop_after: UrlWorkflowStage = UrlWorkflowStage.TRANSCRIBED,
) -> UrlWorkflowResult:
    """Create or safely resume one explicitly authorized Panopto lecture job."""

    if stop_after is UrlWorkflowStage.TRANSCRIBED:
        require_source_asr()
    if not confirm_save:
        raise state_error(
            "SAVE_AUTHORIZATION_CONFIRMATION_REQUIRED",
            "starting this workflow requires --confirm-save and an authorization basis",
        )
    if not authorization_basis.strip():
        raise configuration_error("AUTHORIZATION_BASIS_REQUIRED", "authorization basis is required")
    if re.fullmatch(JOB_ID_PATTERN, job_id) is None:
        raise configuration_error("JOB_ID_INVALID", "job ID is invalid")

    source = _source_for_start(source_locator)
    workspace_path = url_workflow_workspace_path(repository_root, job_id)
    if workspace_path.exists() or workspace_path.is_symlink():
        workspace, job, _, state = _verified_status(workspace_path)
        if job.source.kind is not SourceKind.PANOPTO or job.source.locator != source:
            raise state_error(
                "URL_WORKFLOW_JOB_MISMATCH",
                "the existing job belongs to a different lecture; use a new job ID",
            )
    else:
        workspace, job = initialize_job(
            repository_root,
            job_id=job_id,
            source_kind=SourceKind.PANOPTO,
            source=source,
            task_type=task_type,
            language=language,
            output_format=output_format,
            requirements=requirements,
            purpose=purpose,
            audience=audience,
            primary_input=PrimaryInputKind.VIDEO,
        )
        state = JobState.CREATED

    if state is JobState.CREATED:
        authorize_job(
            workspace.root,
            confirm=True,
            basis=authorization_basis,
            allow_local_processing=True,
            allow_network_acquisition=True,
            allow_external_api=False,
            confirmer_identity=confirmer_identity,
        )
    elif state not in _RESUMABLE_STATES:
        raise state_error(
            "URL_WORKFLOW_STATE_UNSUPPORTED",
            "the workflow can resume only through the TRANSCRIBED state",
        )

    return run_url_workflow(
        workspace.root,
        runtime_config=runtime_config,
        preferred_languages=preferred_languages,
        minimum_caption_coverage=minimum_caption_coverage,
        asr_language=asr_language,
        asr_profile=asr_profile,
        asr_device=asr_device,
        stop_after=stop_after,
    )


def run_url_workflow(
    path: Path,
    *,
    runtime_config: AcquisitionRuntimeConfig | None = None,
    preferred_languages: list[str] | None = None,
    minimum_caption_coverage: float = 0.8,
    asr_language: str | None = None,
    asr_profile: str = "whisper-large-v3",
    asr_device: str = "cuda",
    stop_after: UrlWorkflowStage = UrlWorkflowStage.TRANSCRIBED,
) -> UrlWorkflowResult:
    """Acquire, normalize, and transcribe a verified Panopto job, reusing completed stages."""

    if stop_after is UrlWorkflowStage.TRANSCRIBED:
        require_source_asr()
        if asr_device != "cuda" and not asr_device.startswith("cuda:"):
            raise configuration_error(
                "ASR_GPU_REQUIRED",
                "the Panopto URL workflow requires an explicit CUDA device (cuda or cuda:<index>)",
            )
    workspace, job, status, state = _verified_status(path)
    if stop_after is UrlWorkflowStage.ACQUIRED and job.source.kind is SourceKind.LOCAL_FILE:
        raise configuration_error(
            "URL_WORKFLOW_SOURCE_UNSUPPORTED", "the URL workflow requires a network source"
        )
    if stop_after is UrlWorkflowStage.TRANSCRIBED and job.source.kind is not SourceKind.PANOPTO:
        raise configuration_error(
            "URL_WORKFLOW_SOURCE_UNSUPPORTED",
            "the URL workflow currently supports Panopto jobs only",
        )
    if (
        stop_after is UrlWorkflowStage.TRANSCRIBED
        and primary_input_kind(job) is not PrimaryInputKind.VIDEO
    ):
        raise configuration_error(
            "URL_WORKFLOW_INPUT_UNSUPPORTED",
            "the Panopto frame workflow requires video as its primary input",
        )
    if state not in _RESUMABLE_STATES:
        if state is JobState.CREATED:
            raise state_error(
                "AUTHORIZATION_REQUIRED",
                "explicitly authorize local processing and network acquisition before running this workflow",
            )
        raise state_error(
            "URL_WORKFLOW_STATE_UNSUPPORTED",
            "the workflow can resume only through the TRANSCRIBED state",
        )
    authorization = status.get("authorization", {})
    if not authorization.get("allow_local_processing"):
        raise state_error(
            "LOCAL_PROCESSING_NOT_AUTHORIZED",
            "local processing must be explicitly authorized for this job",
        )
    if not authorization.get("allow_network_acquisition"):
        raise state_error(
            "NETWORK_ACQUISITION_NOT_AUTHORIZED",
            "network acquisition must be explicitly authorized for this job",
        )
    runtime = runtime_config or AcquisitionRuntimeConfig()
    if (
        job.source.kind is SourceKind.PANOPTO
        and runtime.cookies_from_browser is not None
        and not runtime.panopto_ytdlp_fallback
    ):
        # The URL workflow treats an explicitly chosen browser as opt-in to the
        # provider's existing authorized viewer path.
        runtime = runtime.model_copy(update={"panopto_ytdlp_fallback": True})
    runtime.validate_for(job.source.kind)
    if stop_after is UrlWorkflowStage.ACQUIRED:
        if state in {JobState.AUTHORIZED, JobState.ACQUIRED}:
            acquired = acquire_job(workspace.root, runtime)
            artifact = acquired.source_manifest.artifact_record.artifact
            reused = acquired.reused
        else:
            artifact = verify_acquired_bundle(workspace, job)
            reused = True
        return UrlWorkflowResult(
            job_id=job.job_id,
            workspace_root=str(workspace.root),
            state=JobState.ACQUIRED.value if state is JobState.AUTHORIZED else state.value,
            raw_media_path=artifact.relative_storage_path,
            analysis_media_path=None,
            speech_audio_path=None,
            transcript_segments_path=None,
            frame_index_path=None,
            acquisition_reused=reused,
            normalization_reused=False,
            transcription_reused=False,
            frame_count=0,
            frames=[],
            segment_count=0,
            transcript_sources=[],
            asr_backend={},
        )
    transcription_config = TranscriptionConfig(
        preferred_languages=preferred_languages or [],
        minimum_caption_coverage=minimum_caption_coverage,
        asr_language=asr_language,
        asr_profile=asr_profile,
        asr_device=asr_device,
        require_full_asr=True,
    )

    acquisition_reused = state not in {JobState.AUTHORIZED, JobState.ACQUIRED}
    if state in {JobState.AUTHORIZED, JobState.ACQUIRED}:
        acquisition_reused = acquire_job(workspace.root, runtime).reused

    normalization_reused = state in {JobState.NORMALIZED, JobState.TRANSCRIBED}
    if state in {JobState.AUTHORIZED, JobState.ACQUIRED, JobState.NORMALIZED}:
        normalization = normalize_job(workspace.root)
        media_manifest = normalization.media_manifest
        normalization_reused = normalization.reused
    else:
        raw_artifact = verify_acquired_bundle(workspace, job)
        media_manifest = load_normalization_result(workspace, job, raw_artifact).media_manifest

    from video_evidence_pipeline.transcription.container import ContainerAsrBackend

    repository_root = getattr(workspace, "resources_root", workspace.root.parent.parent)
    transcription = transcribe_job(
        workspace.root,
        transcription_config,
        backend_override=ContainerAsrBackend(repository_root, transcription_config),
    )
    manifest = transcription.transcript_manifest
    asr_backend = next((item for item in manifest.backends if item.source.value == "asr"), None)
    if asr_backend is None or not manifest.configuration.require_full_asr:
        raise integrity_error(
            "GPU_ASR_PROVENANCE_MISSING",
            "full ASR was required but the completed transcript has no ASR backend identity",
        )
    if (
        asr_backend.selected_device != "cuda"
        or asr_backend.gpu_index is None
        or asr_backend.gpu_name is None
        or asr_backend.fallback is True
    ):
        raise integrity_error(
            "GPU_ASR_PROVENANCE_INVALID",
            "ASR completed without verifiable CUDA device provenance",
        )

    frames = _frame_rows(media_manifest)
    if not frames:
        raise integrity_error(
            "FRAME_INDEX_UNAVAILABLE",
            "normalized Panopto video has no verified frame records",
        )
    analysis_proxy = getattr(media_manifest, "analysis_proxy", None)
    speech_audio = getattr(media_manifest, "speech_audio", None)
    frame_index_path = _artifact_path(media_manifest, "frame_index_artifact")
    return UrlWorkflowResult(
        job_id=job.job_id,
        workspace_root=str(workspace.root),
        state=JobState.TRANSCRIBED.value,
        raw_media_path=_artifact_path(media_manifest, "raw_artifact"),
        analysis_media_path=(
            _artifact_path(analysis_proxy, "artifact") if analysis_proxy is not None else None
        ),
        speech_audio_path=(
            _artifact_path(speech_audio, "artifact") if speech_audio is not None else None
        ),
        transcript_segments_path=_artifact_path(manifest, "segments_artifact"),
        frame_index_path=frame_index_path,
        acquisition_reused=acquisition_reused,
        normalization_reused=normalization_reused,
        transcription_reused=transcription.reused,
        frame_count=len(frames),
        frames=frames,
        segment_count=manifest.segment_count,
        transcript_sources=[item.source.value for item in manifest.backends],
        asr_backend={
            "profile": manifest.configuration.asr_profile,
            "model_repository": asr_backend.model_repository,
            "model_revision": asr_backend.model_revision,
            "device_policy": asr_backend.device_policy,
            "selected_device": asr_backend.selected_device,
            "gpu_index": asr_backend.gpu_index,
            "gpu_name": asr_backend.gpu_name,
            "total_vram_mib": asr_backend.total_vram_mib,
            "free_vram_at_selection_mib": asr_backend.free_vram_at_selection_mib,
            "compute_type": asr_backend.compute_type,
            "fallback": asr_backend.fallback,
        },
    )
