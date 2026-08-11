"""Source and stage manifests."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRecord,
    ArtifactRef,
    EvidenceLedgerArtifactRef,
    ProducerIdentity,
)
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.media import ToolIdentity
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


class AcquisitionMode(StrEnum):
    PROGRESSIVE = "progressive"
    ADAPTIVE_MERGED = "adaptive-merged"


EvidenceHost = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")]
EvidenceWarning = Annotated[str, Field(min_length=1, max_length=256)]
YtdlpMetadataToken = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._+-]+$"),
]


class YtdlpFormatEvidence(StrictModel):
    format_id: YtdlpMetadataToken
    video_codec: YtdlpMetadataToken
    audio_codec: YtdlpMetadataToken
    protocol: YtdlpMetadataToken


class AcquisitionEvidence(StrictModel):
    adapter_kind: SourceKind
    transport: str = Field(min_length=1, max_length=64)
    backend: str = Field(min_length=1, max_length=128)
    backend_version: str = Field(min_length=1, max_length=128)
    obtained_at: UtcDateTime
    requested_host: EvidenceHost
    final_host: EvidenceHost | None
    redirect_count: int | None = Field(ge=0, le=20)
    http_status_category: HttpStatusCategory
    response_content_type: str | None = Field(default=None, max_length=256)
    declared_content_length: int | None = Field(default=None, ge=0)
    actual_acquired_size: int = Field(ge=1)
    credential_mode: CredentialMode
    acquisition_mode: AcquisitionMode | None = None
    selected_formats: list[YtdlpFormatEvidence] = Field(default_factory=list, max_length=2)
    merger: ToolIdentity | None = None
    warnings: list[EvidenceWarning] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def ytdlp_lineage_is_consistent(self) -> Self:
        if self.backend != "yt-dlp":
            if (
                self.acquisition_mode is not None
                or self.selected_formats
                or self.merger is not None
            ):
                raise ValueError("non-yt-dlp acquisition must not claim yt-dlp lineage")
            return self

        # Source manifests created before adaptive lineage was introduced remain
        # readable for idempotent recovery. New yt-dlp acquisitions always set
        # acquisition_mode and selected_formats in the runner.
        if self.acquisition_mode is None and not self.selected_formats and self.merger is None:
            return self

        if self.acquisition_mode is AcquisitionMode.PROGRESSIVE:
            if (
                len(self.selected_formats) != 1
                or self.selected_formats[0].video_codec == "none"
                or self.selected_formats[0].audio_codec == "none"
                or self.merger is not None
            ):
                raise ValueError("progressive yt-dlp lineage is invalid")
        elif self.acquisition_mode is AcquisitionMode.ADAPTIVE_MERGED:
            if (
                len(self.selected_formats) != 2
                or self.selected_formats[0].video_codec == "none"
                or self.selected_formats[0].audio_codec != "none"
                or self.selected_formats[1].video_codec != "none"
                or self.selected_formats[1].audio_codec == "none"
                or self.merger is None
                or self.merger.name != "ffmpeg"
            ):
                raise ValueError("adaptive yt-dlp lineage is invalid")
        else:
            raise ValueError("yt-dlp acquisition requires a supported acquisition mode")
        return self


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

    @model_validator(mode="after")
    def evidence_matches_source_kind(self) -> Self:
        if self.source_kind is SourceKind.LOCAL_FILE:
            if self.acquisition_evidence is not None:
                raise ValueError("local source manifest must not contain acquisition evidence")
        elif (
            self.acquisition_evidence is None
            or self.acquisition_evidence.adapter_kind is not self.source_kind
        ):
            raise ValueError("network source manifest requires matching acquisition evidence")
        return self


class StageManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    stage: str = Field(min_length=1, max_length=128)
    status: StageStatus
    state_transition: JobState
    input_artifacts: list[EvidenceLedgerArtifactRef | ArtifactRef] = Field(default_factory=list)
    output_artifacts: list[EvidenceLedgerArtifactRef | ArtifactRef] = Field(default_factory=list)
    producer: ProducerIdentity
    configuration_sha256: Sha256
    started_at: UtcDateTime
    finished_at: UtcDateTime
    warnings: list[str] = Field(default_factory=list)
    redacted_error: str | None = Field(default=None, max_length=1024)
