"""Use cases that orchestrate domain, storage, and adapters."""

from __future__ import annotations

import getpass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from video_evidence_pipeline.acquisition.base import AcquisitionResult
from video_evidence_pipeline.acquisition.local_file import verify_acquired_artifact
from video_evidence_pipeline.acquisition.registry import adapter_for
from video_evidence_pipeline.domain.authorization import AuthorizationRecord
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    SourceKind,
    SourceSpec,
    job_fingerprint,
    job_to_yaml,
    load_job_yaml,
)
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import (
    TransitionRecord,
    require_implemented_transition,
    validate_transition_records,
)
from video_evidence_pipeline.errors import VepError, integrity_error, state_error
from video_evidence_pipeline.storage.atomic import (
    atomic_write_json,
    atomic_write_new_bytes,
    read_json_model,
)
from video_evidence_pipeline.storage.jsonl import append_jsonl, read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace


def _load_context(path: Path) -> tuple[JobWorkspace, JobSpec, list[TransitionRecord], JobState]:
    workspace = JobWorkspace.open(path)
    job = load_job_yaml(workspace.job_yaml)
    if job.job_id != workspace.root.name:
        raise integrity_error("JOB_WORKSPACE_MISMATCH", "job ID does not match workspace name")
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    state = validate_transition_records(transitions)
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
) -> tuple[JobWorkspace, JobSpec]:
    locator = (
        str(Path(source).expanduser().absolute())
        if source_kind is SourceKind.LOCAL_FILE
        else source
    )
    job = JobSpec(
        job_id=job_id,
        created_at=datetime.now(UTC),
        source=SourceSpec(kind=source_kind, locator=locator),
        assignment=AssignmentSpec(task_type=task_type, language=language),
        output=OutputSpec(format=output_format),
    )
    workspace = JobWorkspace.create(repository_root, job.job_id)
    atomic_write_new_bytes(workspace.job_yaml, job_to_yaml(job).encode("utf-8"))
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
    workspace, job, _, state = _load_context(path)
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
    allow_external_api: bool,
    confirmer_identity: str | None,
) -> AuthorizationRecord:
    if not confirm:
        raise state_error(
            "AUTHORIZATION_CONFIRMATION_REQUIRED",
            "authorization requires explicit --confirm",
        )
    workspace, job, transitions, state = _load_context(path)
    if state not in {JobState.CREATED, JobState.AUTHORIZED}:
        raise state_error("STATE_GATE_FAILED", "job cannot be authorized in its current state")
    record = AuthorizationRecord(
        job_id=job.job_id,
        job_fingerprint=job_fingerprint(job),
        confirmed_by_user=True,
        authorization_basis=basis,
        allow_local_processing=allow_local_processing,
        allow_external_api=allow_external_api,
        confirmed_at=datetime.now(UTC),
        confirmer_identity=confirmer_identity or getpass.getuser() or "local-user",
    )
    atomic_write_json(workspace.authorization_json, record)
    if state is JobState.CREATED:
        _append_transition(
            workspace,
            job,
            transitions,
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


def _require_current_authorization(job: JobSpec, authorization: AuthorizationRecord) -> None:
    if authorization.job_id != job.job_id or authorization.job_fingerprint != job_fingerprint(job):
        raise state_error(
            "AUTHORIZATION_STALE",
            "authorization no longer matches the normalized job specification",
        )
    if not authorization.allow_local_processing:
        raise state_error(
            "LOCAL_PROCESSING_NOT_AUTHORIZED",
            "local processing was not explicitly authorized",
        )


def acquire_job(path: Path) -> AcquisitionResult:
    workspace, job, transitions, state = _load_context(path)
    if state not in {JobState.AUTHORIZED, JobState.ACQUIRED}:
        raise state_error("STATE_GATE_FAILED", "job must be AUTHORIZED before acquisition")
    authorization = _authorization(workspace)
    _require_current_authorization(job, authorization)
    result = adapter_for(job.source).acquire(job, workspace)
    if state is JobState.AUTHORIZED:
        _append_transition(
            workspace,
            job,
            transitions,
            from_state=JobState.AUTHORIZED,
            to_state=JobState.ACQUIRED,
            trigger="acquire.local_file",
        )
    return result


def _append_transition(
    workspace: JobWorkspace,
    job: JobSpec,
    records: list[TransitionRecord],
    *,
    from_state: JobState,
    to_state: JobState,
    trigger: str,
) -> None:
    require_implemented_transition(from_state, to_state)
    if validate_transition_records(records) is not from_state:
        raise state_error("STATE_GATE_FAILED", "stored state changed before transition")
    record = TransitionRecord(
        sequence=records[-1].sequence + 1,
        job_id=job.job_id,
        from_state=from_state,
        to_state=to_state,
        trigger=trigger,
        occurred_at=datetime.now(UTC),
        job_fingerprint=job_fingerprint(job),
    )
    append_jsonl(workspace.transitions_jsonl, record)


def job_status(path: Path) -> tuple[dict[str, Any], int]:
    workspace, job, _, state = _load_context(path)
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
            authorization_status.update(
                {
                    "valid": True,
                    "stale": authorization.job_fingerprint != current_fingerprint,
                    "allow_local_processing": authorization.allow_local_processing,
                    "allow_external_api": authorization.allow_external_api,
                }
            )
            if authorization_status["stale"]:
                exit_code = 3
        except VepError as exc:
            authorization_status.update({"valid": False, "error_code": exc.code})
            exit_code = int(exc.exit_code)

    artifact_status: dict[str, Any] = {"status": "not-applicable"}
    if state is JobState.ACQUIRED:
        try:
            artifact = verify_acquired_artifact(workspace)
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
