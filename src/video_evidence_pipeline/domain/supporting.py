"""Bounded supporting-input contracts for transcript refinement and evidence."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.job import SupportingInputKind


class RenderedPdfPage(StrictModel):
    page_number: int = Field(ge=1, le=10_000)
    artifact: ArtifactRef


class SupportingInputRecord(StrictModel):
    input_id: str = Field(pattern=r"^S[0-9]{6}$")
    kind: SupportingInputKind
    original_artifact: ArtifactRef
    canonical_text_artifact: ArtifactRef | None = None
    rendered_pages: list[RenderedPdfPage] = Field(default_factory=list, max_length=32)
    warnings: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("warnings")
    @classmethod
    def warnings_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(not item.strip() for item in value):
            raise ValueError("supporting input warnings must be sorted, unique, and non-blank")
        return value

    @model_validator(mode="after")
    def artifact_shape_matches_kind(self) -> Self:
        page_numbers = [item.page_number for item in self.rendered_pages]
        if page_numbers != sorted(set(page_numbers)):
            raise ValueError("rendered PDF pages must be sorted and unique")
        if self.kind is SupportingInputKind.PDF:
            return self
        if self.rendered_pages:
            raise ValueError("only PDF supporting inputs may contain rendered pages")
        if self.kind is SupportingInputKind.IMAGE and self.canonical_text_artifact is not None:
            raise ValueError("image supporting input must not claim OCR text")
        if self.kind is SupportingInputKind.NOTES and self.canonical_text_artifact is None:
            raise ValueError("notes supporting input requires canonical text")
        return self


class SupportingInputManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    supporting_fingerprint: Sha256
    implementation_version: str = Field(min_length=1, max_length=128)
    inputs: list[SupportingInputRecord] = Field(default_factory=list, max_length=32)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def inputs_are_canonical(self) -> Self:
        identifiers = [item.input_id for item in self.inputs]
        if identifiers != [f"S{index:06d}" for index in range(1, len(identifiers) + 1)]:
            raise ValueError("supporting input IDs must be contiguous")
        identities = [
            (item.kind.value, item.original_artifact.content_sha256) for item in self.inputs
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("supporting inputs must be unique")
        return self
