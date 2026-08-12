"""Strict raw-to-refined transcript exchange contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.evidence import SourceRef, SourceRefKind
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.transcription import TranscriptSegment


class RefinementEditKind(StrEnum):
    AUTOMATIC = "automatic"
    SEMANTIC = "semantic"


class RefinementExecutionMode(StrEnum):
    EXTERNAL_EXCHANGE = "external_exchange"
    TEST_FIXTURE = "test_fixture"


class RefinerIdentity(StrictModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    executor: str = Field(min_length=1, max_length=128)
    execution_mode: RefinementExecutionMode
    external_api_used: StrictBool

    @field_validator("provider", "model", "version", "executor")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("refiner identity metadata must not be blank")
        return value


class RefinementEdit(StrictModel):
    edit_id: str = Field(pattern=r"^R[0-9]{6}$")
    segment_id: str = Field(pattern=r"^T[0-9]{6}$")
    original_segment_fingerprint: Sha256
    refined_text: str = Field(min_length=1, max_length=65_536)
    kind: RefinementEditKind
    source_refs: list[SourceRef] = Field(min_length=1, max_length=128)

    @field_validator("refined_text")
    @classmethod
    def refined_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("refined text must not be blank")
        return value

    @field_validator("source_refs")
    @classmethod
    def refs_are_canonical(cls, value: list[SourceRef]) -> list[SourceRef]:
        keys = [(item.kind.value, item.ref_id) for item in value]
        if keys != sorted(set(keys)):
            raise ValueError("refinement source references must be sorted and unique")
        return value

    @model_validator(mode="after")
    def semantic_edits_require_non_transcript_grounding(self) -> Self:
        supporting_kinds = {
            SourceRefKind.APPROVED_ARTIFACT,
            SourceRefKind.FRAME,
            SourceRefKind.PDF_TEXT,
            SourceRefKind.PDF_PAGE,
            SourceRefKind.IMAGE,
            SourceRefKind.NOTES,
        }
        if self.kind is RefinementEditKind.AUTOMATIC:
            if any(item.kind is not SourceRefKind.TRANSCRIPT_SEGMENT for item in self.source_refs):
                raise ValueError("automatic refinement must only cite its transcript segment")
        elif not any(item.kind in supporting_kinds for item in self.source_refs):
            raise ValueError("semantic refinement requires supporting source grounding")
        return self


class RefinementAmbiguity(StrictModel):
    ambiguity_id: str = Field(pattern=r"^RA[0-9]{6}$")
    segment_id: str = Field(pattern=r"^T[0-9]{6}$")
    description: str = Field(min_length=1, max_length=65_536)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=128)

    @field_validator("description")
    @classmethod
    def description_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("refinement ambiguity must not be blank")
        return value

    @field_validator("source_refs")
    @classmethod
    def refs_are_canonical(cls, value: list[SourceRef]) -> list[SourceRef]:
        keys = [(item.kind.value, item.ref_id) for item in value]
        if keys != sorted(set(keys)):
            raise ValueError("refinement ambiguity references must be sorted and unique")
        return value


class RefinementRequestManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    request_fingerprint: Sha256
    raw_transcript_fingerprint: Sha256
    raw_segments_artifact: ArtifactRef
    transcript_segments: list[TranscriptSegment] = Field(min_length=1, max_length=100_000)
    available_support_artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=1024)
    available_source_refs: list[SourceRef] = Field(min_length=1, max_length=200_000)
    semantic_correction_contract: Literal[
        "Each semantic edit must refine exactly one transcript segment and cite a non-transcript supporting source; otherwise return one ambiguity and do not merge propositions."
    ] = "Each semantic edit must refine exactly one transcript segment and cite a non-transcript supporting source; otherwise return one ambiguity and do not merge propositions."
    created_at: UtcDateTime

    @model_validator(mode="after")
    def catalogs_are_canonical(self) -> Self:
        identifiers = [item.segment_id for item in self.transcript_segments]
        if identifiers != [f"T{index:06d}" for index in range(1, len(identifiers) + 1)]:
            raise ValueError("refinement transcript segments must be contiguous")
        ref_keys = [(item.kind.value, item.ref_id) for item in self.available_source_refs]
        if ref_keys != sorted(set(ref_keys)):
            raise ValueError("refinement source catalog must be sorted and unique")
        artifact_ids = [item.artifact_id for item in self.available_support_artifacts]
        if artifact_ids != sorted(set(artifact_ids)):
            raise ValueError("refinement support artifacts must be sorted and unique")
        return self


class RefinementResponseManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_fingerprint: Sha256
    refiner: RefinerIdentity
    semantic_edits: list[RefinementEdit] = Field(default_factory=list, max_length=100_000)
    ambiguities: list[RefinementAmbiguity] = Field(default_factory=list, max_length=100_000)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def outputs_are_canonical(self) -> Self:
        edit_ids = [item.edit_id for item in self.semantic_edits]
        if edit_ids != [f"R{index:06d}" for index in range(1, len(edit_ids) + 1)]:
            raise ValueError("semantic refinement edit IDs must be contiguous")
        edited_segments = [item.segment_id for item in self.semantic_edits]
        if len(set(edited_segments)) != len(edited_segments):
            raise ValueError("semantic refinement may edit each segment once")
        ambiguity_ids = [item.ambiguity_id for item in self.ambiguities]
        if ambiguity_ids != sorted(set(ambiguity_ids)):
            raise ValueError("refinement ambiguity IDs must be sorted and unique")
        if {item.segment_id for item in self.semantic_edits} & {
            item.segment_id for item in self.ambiguities
        }:
            raise ValueError("a segment cannot be both refined semantically and unresolved")
        return self


class RefinementEditLedger(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    records: list[RefinementEdit] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def records_are_canonical(self) -> Self:
        identifiers = [item.edit_id for item in self.records]
        if identifiers != [f"R{index:06d}" for index in range(1, len(identifiers) + 1)]:
            raise ValueError("refinement edit IDs must be contiguous")
        segments = [item.segment_id for item in self.records]
        if len(set(segments)) != len(segments):
            raise ValueError("refinement ledger may edit each segment once")
        return self


class RefinementAmbiguityLedger(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    records: list[RefinementAmbiguity] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def records_are_canonical(self) -> Self:
        identifiers = [item.ambiguity_id for item in self.records]
        if identifiers != sorted(set(identifiers)):
            raise ValueError("refinement ambiguity IDs must be sorted and unique")
        return self


class RefinementManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    raw_transcript_fingerprint: Sha256
    refined_transcript_fingerprint: Sha256
    request_fingerprint: Sha256
    response_fingerprint: Sha256
    implementation_version: str = Field(min_length=1, max_length=128)
    raw_segments_artifact: ArtifactRef
    refined_segments_artifact: ArtifactRef
    edit_ledger_artifact: ArtifactRef
    ambiguity_artifact: ArtifactRef
    response_artifact: ArtifactRef
    edit_count: int = Field(ge=0)
    ambiguity_count: int = Field(ge=0)
    started_at: UtcDateTime
    created_at: UtcDateTime

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> Self:
        if self.created_at < self.started_at:
            raise ValueError("refinement manifest timestamps are inconsistent")
        return self
