"""Append-only state transition contracts and policy."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.errors import integrity_error, state_error


class TransitionRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=1)
    job_id: JobId
    from_state: JobState | None
    to_state: JobState
    trigger: str = Field(min_length=1, max_length=128)
    occurred_at: UtcDateTime
    job_fingerprint: Sha256
    warnings: list[str] = Field(default_factory=list)


ALLOWED_TRANSITIONS: set[tuple[JobState, JobState]] = {
    (JobState.CREATED, JobState.AUTHORIZED),
    (JobState.AUTHORIZED, JobState.ACQUIRED),
    (JobState.ACQUIRED, JobState.NORMALIZED),
    (JobState.NORMALIZED, JobState.TRANSCRIBED),
    (JobState.TRANSCRIBED, JobState.EVIDENCE_READY),
    (JobState.EVIDENCE_READY, JobState.DRAFTED),
    (JobState.DRAFTED, JobState.AUDITED),
    (JobState.AUDITED, JobState.REVISION_REQUIRED),
    (JobState.REVISION_REQUIRED, JobState.DRAFTED),
    (JobState.AUDITED, JobState.HUMAN_REVIEW_READY),
    (JobState.HUMAN_REVIEW_READY, JobState.HUMAN_APPROVED),
    (JobState.HUMAN_APPROVED, JobState.EXPORTED),
}

for _state in JobState:
    if _state not in {JobState.EXPORTED, JobState.CANCELLED, JobState.FAILED}:
        ALLOWED_TRANSITIONS.add((_state, JobState.BLOCKED))
        ALLOWED_TRANSITIONS.add((_state, JobState.FAILED))
    if _state not in {JobState.EXPORTED, JobState.CANCELLED}:
        ALLOWED_TRANSITIONS.add((_state, JobState.CANCELLED))

IMPLEMENTED_TRANSITIONS = {
    (JobState.CREATED, JobState.AUTHORIZED),
    (JobState.AUTHORIZED, JobState.ACQUIRED),
}


def validate_transition_records(records: list[TransitionRecord]) -> JobState:
    if not records:
        raise integrity_error("TRANSITION_LOG_EMPTY", "transition log is empty")
    first = records[0]
    if (
        first.sequence != 1
        or first.from_state is not None
        or first.to_state is not JobState.CREATED
    ):
        raise integrity_error("TRANSITION_LOG_INVALID", "transition log does not begin at CREATED")
    previous = first
    for record in records[1:]:
        if record.sequence != previous.sequence + 1:
            raise integrity_error("TRANSITION_LOG_INVALID", "transition sequence is not contiguous")
        if record.job_id != previous.job_id or record.from_state is not previous.to_state:
            raise integrity_error("TRANSITION_LOG_INVALID", "transition lineage is inconsistent")
        if (record.from_state, record.to_state) not in ALLOWED_TRANSITIONS:
            raise integrity_error("TRANSITION_LOG_INVALID", "transition is not allowed")
        previous = record
    return previous.to_state


def require_implemented_transition(from_state: JobState, to_state: JobState) -> None:
    if (from_state, to_state) not in IMPLEMENTED_TRANSITIONS:
        raise state_error("STAGE_NOT_IMPLEMENTED", "requested state transition is not implemented")
