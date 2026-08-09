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
from video_evidence_pipeline.domain.state import JobState
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
from video_evidence_pipeline.storage.atomic import (
    atomic_write_json,
    atomic_write_new_bytes,
    read_json_model,
)
from video_evidence_pipeline.storage.jsonl import append_jsonl, exclusive_append_lock, read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace


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
    if state is JobState.ACQUIRED:
        verify_acquired_bundle(workspace, job)
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
    if state is JobState.ACQUIRED:
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
    return (
        {
            "job_id": job.job_id,
            "stored_state": state.value,
            "job_fingerprint": current_fingerprint,
            "authorization": authorization_status,
            "artifact_integrity": artifact_status,
        },
        exit_code,
    )
