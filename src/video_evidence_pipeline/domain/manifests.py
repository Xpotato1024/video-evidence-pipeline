"""Source and stage manifests."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRecord, ArtifactRef, ProducerIdentity
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.state import JobState


class StageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SourceManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    source_kind: SourceKind
    display_name: str = Field(min_length=1, max_length=128)
    locator_fingerprint: Sha256
    artifact_record: ArtifactRecord
    created_at: UtcDateTime
    warnings: list[str] = Field(default_factory=list)


class StageManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    stage: str = Field(min_length=1, max_length=128)
    status: StageStatus
    state_transition: JobState
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    output_artifacts: list[ArtifactRef] = Field(default_factory=list)
    producer: ProducerIdentity
    configuration_sha256: Sha256
    started_at: UtcDateTime
    finished_at: UtcDateTime
    warnings: list[str] = Field(default_factory=list)
    redacted_error: str | None = Field(default=None, max_length=1024)
