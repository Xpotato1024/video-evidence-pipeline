"""Source and stage manifests."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRecord, ArtifactRef, ProducerIdentity
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.state import JobState


class StageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CredentialMode(StrEnum):
    NONE = "none"
    ENVIRONMENT_TOKEN = "environment-token"
    BROWSER_COOKIE = "browser-cookie"


class HttpStatusCategory(StrEnum):
    SUCCESS = "2xx"
    NOT_OBSERVED = "not-observed"


EvidenceHost = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")]
EvidenceWarning = Annotated[str, Field(min_length=1, max_length=256)]


class AcquisitionEvidence(StrictModel):
    adapter_kind: SourceKind
    transport: str = Field(min_length=1, max_length=64)
    backend: str = Field(min_length=1, max_length=128)
    backend_version: str = Field(min_length=1, max_length=128)
    obtained_at: UtcDateTime
    requested_host: EvidenceHost
    final_host: EvidenceHost
    redirect_count: int = Field(ge=0, le=20)
    http_status_category: HttpStatusCategory
    response_content_type: str | None = Field(default=None, max_length=256)
    declared_content_length: int | None = Field(default=None, ge=0)
    actual_acquired_size: int = Field(ge=1)
    credential_mode: CredentialMode
    warnings: list[EvidenceWarning] = Field(default_factory=list, max_length=32)


class SourceManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    source_kind: SourceKind
    display_name: str = Field(min_length=1, max_length=128)
    locator_fingerprint: Sha256
    artifact_record: ArtifactRecord
    acquisition_evidence: AcquisitionEvidence | None = None
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
