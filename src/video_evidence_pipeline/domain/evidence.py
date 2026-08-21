"""Strict semantic evidence, ambiguity, and analyzer exchange contracts."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef, EvidenceLedgerArtifactRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.media import FrameRecord
from video_evidence_pipeline.domain.semantic import SkillSnapshot
from video_evidence_pipeline.domain.transcription import TranscriptSegment


class InterpretationLevel(StrEnum):
    VERBATIM = "verbatim"
    EXPLICIT_STATEMENT = "explicit_statement"
    VISUAL_OBSERVATION = "visual_observation"
    REASONABLE_INFERENCE = "reasonable_inference"
    SPECULATION = "speculation"
    USER_OPINION = "user_opinion"
    EXTERNAL_FACT = "external_fact"


class SourceRefKind(StrEnum):
    TRANSCRIPT_SEGMENT = "transcript_segment"
    FRAME = "frame"
    APPROVED_ARTIFACT = "approved_artifact"
    PDF_TEXT = "pdf_text"
    PDF_PAGE = "pdf_page"
    IMAGE = "image"
    NOTES = "notes"


class EvidenceConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    NEEDS_REVIEW = "needs_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AmbiguityStatus(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class AnalyzerExecutionMode(StrEnum):
    EXTERNAL_EXCHANGE = "external_exchange"
    TEST_FIXTURE = "test_fixture"


AtomicPropositionContract = Literal[
    "One record states one atomic proposition; every reference supports that same proposition; do not combine separate facts."
]
ATOMIC_PROPOSITION_RULE = (
    "One record states one atomic proposition; every reference supports that same proposition; "
    "do not combine separate facts."
)
EVIDENCE_INFORMATION_ROLES = frozenset(
    {
        "role:action-item",
        "role:condition",
        "role:conclusion",
        "role:context",
        "role:deadline",
        "role:decision",
        "role:emphasis",
        "role:example",
        "role:exception",
        "role:instruction",
        "role:open-question",
        "role:owner",
        "role:prohibition",
        "role:rationale",
        "role:safety",
        "role:thesis",
    }
)


class SourceRef(StrictModel):
    kind: SourceRefKind
    ref_id: str = Field(min_length=1, max_length=128)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    content_sha256: Sha256 = Field(
        description="Transcript-record semantic fingerprint or canonical frame artifact SHA-256"
    )

    @model_validator(mode="after")
    def shape_matches_kind(self) -> Self:
        if self.kind is SourceRefKind.TRANSCRIPT_SEGMENT:
            if (
                len(self.ref_id) != 7
                or not self.ref_id.startswith("T")
                or not self.ref_id[1:].isdigit()
                or ((self.start_ms is None) != (self.end_ms is None))
                or (
                    self.start_ms is not None
                    and self.end_ms is not None
                    and self.end_ms <= self.start_ms
                )
            ):
                raise ValueError("transcript source reference is invalid")
        elif self.kind is SourceRefKind.FRAME and (
            len(self.ref_id) != 7
            or not self.ref_id.startswith("F")
            or not self.ref_id[1:].isdigit()
            or self.start_ms is None
            or self.end_ms is not None
        ):
            raise ValueError("frame source reference is invalid")
        if self.kind in {
            SourceRefKind.APPROVED_ARTIFACT,
            SourceRefKind.PDF_TEXT,
            SourceRefKind.PDF_PAGE,
            SourceRefKind.IMAGE,
            SourceRefKind.NOTES,
        } and (self.start_ms is not None or self.end_ms is not None):
            raise ValueError("supporting artifact reference must not invent a timestamp")
        if self.kind in {SourceRefKind.PDF_TEXT, SourceRefKind.IMAGE, SourceRefKind.NOTES} and (
            len(self.ref_id) != 7
            or not self.ref_id.startswith("S")
            or not self.ref_id[1:].isdigit()
        ):
            raise ValueError("supporting artifact reference is invalid")
        if self.kind is SourceRefKind.PDF_PAGE and (
            re.fullmatch(r"S[0-9]{6}:P[0-9]{4}", self.ref_id) is None
        ):
            raise ValueError("PDF page reference is invalid")
        return self


class EvidenceRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: str = Field(pattern=r"^E[0-9]{6}$")
    observation: str = Field(min_length=1, max_length=65536)
    interpretation_level: InterpretationLevel
    confidence: EvidenceConfidence
    source_refs: list[SourceRef] = Field(min_length=1, max_length=128)
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    asserted_as_video_fact: StrictBool = False
    flags: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("observation")
    @classmethod
    def observation_is_not_blank(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("evidence observation must not be blank")
        return value

    @field_validator("source_refs")
    @classmethod
    def refs_are_unique(cls, value: list[SourceRef]) -> list[SourceRef]:
        keys = [(item.kind.value, item.ref_id) for item in value]
        if len(set(keys)) != len(keys) or keys != sorted(keys):
            raise ValueError("evidence source references must be sorted and unique")
        return value

    @field_validator("flags")
    @classmethod
    def flags_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(not item.strip() for item in value):
            raise ValueError("evidence flags must be sorted, unique, and non-blank")
        unknown_roles = [item for item in value if item.startswith("role:")]
        if any(item not in EVIDENCE_INFORMATION_ROLES for item in unknown_roles):
            raise ValueError("unknown evidence information role")
        return value

    @model_validator(mode="after")
    def interpretation_is_semantically_safe(self) -> Self:
        direct_video_levels = {
            InterpretationLevel.VERBATIM,
            InterpretationLevel.EXPLICIT_STATEMENT,
            InterpretationLevel.VISUAL_OBSERVATION,
        }
        if self.asserted_as_video_fact and self.interpretation_level not in direct_video_levels:
            raise ValueError("non-direct interpretation cannot be asserted as video fact")
        if self.asserted_as_video_fact and any(
            item.kind is SourceRefKind.NOTES for item in self.source_refs
        ):
            raise ValueError("notes cannot be asserted as a video fact")
        if self.interpretation_level in {
            InterpretationLevel.VERBATIM,
            InterpretationLevel.EXPLICIT_STATEMENT,
        } and not any(item.kind is SourceRefKind.TRANSCRIPT_SEGMENT for item in self.source_refs):
            raise ValueError("verbal evidence requires a transcript source")
        if self.interpretation_level is InterpretationLevel.VISUAL_OBSERVATION and not any(
            item.kind is SourceRefKind.FRAME for item in self.source_refs
        ):
            raise ValueError("visual observation requires a frame source")
        if self.interpretation_level is InterpretationLevel.EXTERNAL_FACT and not any(
            item.kind
            in {
                SourceRefKind.APPROVED_ARTIFACT,
                SourceRefKind.PDF_TEXT,
                SourceRefKind.PDF_PAGE,
                SourceRefKind.IMAGE,
                SourceRefKind.NOTES,
            }
            for item in self.source_refs
        ):
            raise ValueError("external fact requires an approved artifact source")
        return self


class AmbiguityRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    ambiguity_id: str = Field(pattern=r"^A[0-9]{6}$")
    description: str = Field(min_length=1, max_length=65536)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=128)
    status: AmbiguityStatus = AmbiguityStatus.UNRESOLVED
    resolution: str | None = Field(default=None, min_length=1, max_length=65536)
    review_status: ReviewStatus = ReviewStatus.NEEDS_REVIEW

    @field_validator("source_refs")
    @classmethod
    def refs_are_canonical(cls, value: list[SourceRef]) -> list[SourceRef]:
        keys = [(item.kind.value, item.ref_id) for item in value]
        if len(set(keys)) != len(keys) or keys != sorted(keys):
            raise ValueError("ambiguity source references must be sorted and unique")
        return value

    @model_validator(mode="after")
    def resolution_matches_status(self) -> Self:
        if not self.description.strip() or (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("ambiguity description must not be blank")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("ambiguity timestamps are invalid")
        if self.start_ms is None and not self.source_refs:
            raise ValueError("timestamp-less ambiguity requires a source reference")
        if self.status is AmbiguityStatus.UNRESOLVED and self.resolution is not None:
            raise ValueError("unresolved ambiguity cannot contain a resolution")
        if self.status is AmbiguityStatus.RESOLVED and (
            self.resolution is None or not self.resolution.strip()
        ):
            raise ValueError("resolved ambiguity requires a resolution")
        return self


class AnalyzerContract(StrictModel):
    name: Literal["video-evidence-analyzer"] = "video-evidence-analyzer"
    version: Literal["1.0"] = "1.0"
    response_schema_version: Literal["1.0"] = "1.0"
    atomic_proposition_contract: AtomicPropositionContract | None = Field(
        default=None,
        description="Semantic request rule for new input/refinement jobs; absent in v1 requests.",
    )


class AnalyzerIdentity(StrictModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    executor: str = Field(min_length=1, max_length=128)
    execution_mode: AnalyzerExecutionMode
    external_api_used: StrictBool

    @field_validator("provider", "model", "version", "executor")
    @classmethod
    def metadata_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("analyzer metadata must not be blank")
        return value


class AssignmentRequirement(StrictModel):
    requirement_id: str = Field(pattern=r"^R[0-9]{6}$")
    text: str = Field(min_length=1, max_length=512)

    @field_validator("text")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assignment requirement must not be blank")
        return value


class EvidenceAssignmentContext(StrictModel):
    task_type: str = Field(min_length=1, max_length=128)
    language: str = Field(min_length=2, max_length=32)
    requirements: list[AssignmentRequirement] = Field(default_factory=list, max_length=100)
    purpose: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        exclude_if=lambda value: value is None,
    )
    audience: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def requirements_are_contiguous(self) -> Self:
        identifiers = [item.requirement_id for item in self.requirements]
        if identifiers != [f"R{index:06d}" for index in range(1, len(identifiers) + 1)]:
            raise ValueError("assignment requirement IDs must be contiguous")
        return self


class EvidenceRequestManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    request_fingerprint: Sha256
    transcript_bundle_fingerprint: Sha256
    media_bundle_fingerprint: Sha256
    duration_ms: int | None = Field(default=None, gt=0)
    assignment: EvidenceAssignmentContext
    analyzer_contract: AnalyzerContract
    transcript_segments: list[TranscriptSegment] = Field(min_length=1, max_length=100_000)
    transcript_refs: list[SourceRef] = Field(min_length=1, max_length=100_000)
    frame_records: list[FrameRecord] = Field(default_factory=list, max_length=100_000)
    frame_refs: list[SourceRef] = Field(default_factory=list, max_length=100_000)
    approved_artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=1024)
    approved_artifact_refs: list[SourceRef] = Field(default_factory=list, max_length=1024)
    unresolved_ambiguities: list[AmbiguityRecord] = Field(default_factory=list, max_length=100_000)
    skill: SkillSnapshot | None = Field(default=None, exclude_if=lambda value: value is None)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def catalogs_are_canonical(self) -> Self:
        segment_ids = [item.segment_id for item in self.transcript_segments]
        if segment_ids != [f"T{index:06d}" for index in range(1, len(segment_ids) + 1)]:
            raise ValueError("request transcript segments must be contiguous")
        if [item.ref_id for item in self.transcript_refs] != segment_ids or any(
            item.kind is not SourceRefKind.TRANSCRIPT_SEGMENT
            or item.start_ms != segment.start_ms
            or item.end_ms != segment.end_ms
            for item, segment in zip(self.transcript_refs, self.transcript_segments, strict=True)
        ):
            raise ValueError("request transcript reference catalog is inconsistent")
        if self.duration_ms is None:
            if (
                any(
                    item.start_ms is not None or item.end_ms is not None
                    for item in self.transcript_segments
                )
                or self.frame_records
                or self.frame_refs
            ):
                raise ValueError("timestamp-less request cannot claim media timestamps")
        elif any(
            item.end_ms is None or item.end_ms > self.duration_ms
            for item in self.transcript_segments
        ):
            raise ValueError("request transcript timestamp exceeds media duration")
        frame_ids = [item.frame_id for item in self.frame_records]
        if frame_ids != [f"F{index:06d}" for index in range(1, len(frame_ids) + 1)]:
            raise ValueError("request frame records must be contiguous")
        if len(self.frame_records) != len(self.frame_refs) or any(
            source.kind is not SourceRefKind.FRAME
            or source.ref_id != record.frame_id
            or source.start_ms != record.timestamp_ms
            or source.end_ms is not None
            or source.content_sha256 != record.artifact.content_sha256
            for source, record in zip(self.frame_refs, self.frame_records, strict=True)
        ):
            raise ValueError("request frame records and references are inconsistent")
        if self.duration_ms is not None and any(
            item.start_ms > self.duration_ms for item in self.frame_refs
        ):
            raise ValueError("request frame timestamp exceeds media duration")
        artifact_ids = [item.artifact_id for item in self.approved_artifacts]
        if artifact_ids != sorted(set(artifact_ids)):
            raise ValueError("approved artifacts must be sorted and unique")
        if len(self.approved_artifacts) != len(self.approved_artifact_refs) or any(
            source.kind
            not in {
                SourceRefKind.APPROVED_ARTIFACT,
                SourceRefKind.PDF_TEXT,
                SourceRefKind.PDF_PAGE,
                SourceRefKind.IMAGE,
                SourceRefKind.NOTES,
            }
            or (
                source.kind is SourceRefKind.APPROVED_ARTIFACT
                and source.ref_id != artifact.artifact_id
            )
            or source.start_ms is not None
            or source.end_ms is not None
            or source.content_sha256 != artifact.content_sha256
            for source, artifact in zip(
                self.approved_artifact_refs, self.approved_artifacts, strict=True
            )
        ):
            raise ValueError("approved artifact references are inconsistent")
        ambiguity_ids = [item.ambiguity_id for item in self.unresolved_ambiguities]
        if len(set(ambiguity_ids)) != len(ambiguity_ids) or ambiguity_ids != sorted(ambiguity_ids):
            raise ValueError("request ambiguity IDs must be sorted and unique")
        if any(
            item.status is not AmbiguityStatus.UNRESOLVED for item in self.unresolved_ambiguities
        ):
            raise ValueError("request ambiguities must remain unresolved")
        if self.duration_ms is not None and any(
            item.end_ms is not None and item.end_ms > self.duration_ms
            for item in self.unresolved_ambiguities
        ):
            raise ValueError("request ambiguity exceeds media duration")
        return self


class EvidenceResponseManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_fingerprint: Sha256
    analyzer: AnalyzerIdentity
    evidence_records: list[EvidenceRecord] = Field(max_length=100_000)
    ambiguity_records: list[AmbiguityRecord] = Field(default_factory=list, max_length=100_000)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def record_ids_are_contiguous(self) -> Self:
        evidence_ids = [item.evidence_id for item in self.evidence_records]
        if evidence_ids != [f"E{index:06d}" for index in range(1, len(evidence_ids) + 1)]:
            raise ValueError("evidence IDs must be unique and contiguous")
        ambiguity_ids = [item.ambiguity_id for item in self.ambiguity_records]
        if len(set(ambiguity_ids)) != len(ambiguity_ids) or ambiguity_ids != sorted(ambiguity_ids):
            raise ValueError("ambiguity IDs must be sorted and unique")
        return self


class AmbiguityLedger(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    records: list[AmbiguityRecord] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def record_ids_are_sorted_unique(self) -> Self:
        identifiers = [item.ambiguity_id for item in self.records]
        if identifiers != sorted(set(identifiers)):
            raise ValueError("ambiguity ledger IDs must be sorted and unique")
        return self


class EvidenceManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    evidence_bundle_fingerprint: Sha256
    transcript_bundle_fingerprint: Sha256
    media_bundle_fingerprint: Sha256
    request_fingerprint: Sha256
    response_fingerprint: Sha256
    response_created_at: UtcDateTime
    implementation_version: str = Field(min_length=1, max_length=128)
    analyzer: AnalyzerIdentity
    response_artifact: ArtifactRef
    evidence_artifact: EvidenceLedgerArtifactRef
    ambiguity_artifact: ArtifactRef
    evidence_count: int = Field(ge=0)
    ambiguity_count: int = Field(ge=0)
    started_at: UtcDateTime
    created_at: UtcDateTime

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> Self:
        if self.created_at < self.started_at:
            raise ValueError("evidence manifest timestamps are inconsistent")
        return self
