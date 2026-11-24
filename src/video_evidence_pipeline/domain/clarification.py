"""Out-of-band clarifications attached to immutable evidence."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.evidence import SourceRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_192)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class Confirmation(StrictModel):
    provided_by: Name
    statement: Text


class ClarificationInput(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_bundle_fingerprint: Sha256
    ambiguity_id: str = Field(pattern=r"^A[0-9]{6}$")
    decision: Literal["resolved", "supplemented", "deferred"]
    rationale: Text
    resolution: Text | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=32)
    confirmation: Confirmation | None = None
    supersedes: Sha256 | None = None

    @model_validator(mode="after")
    def check_disposition(self) -> Self:
        refs = [(ref.kind, ref.ref_id) for ref in self.source_refs]
        if refs != sorted(set(refs)):
            raise ValueError("clarification references must be sorted and unique")
        if self.decision == "deferred":
            if self.resolution is not None or self.confirmation is not None or self.supersedes:
                raise ValueError("deferred clarifications may only explain an open question")
        elif self.resolution is None:
            raise ValueError("a resolution or supplement must include its text")
        if self.decision == "resolved" and not (self.source_refs or self.confirmation):
            raise ValueError("resolution requires verified evidence or attributed confirmation")
        if self.decision != "resolved" and self.supersedes is not None:
            raise ValueError("only a resolution can supersede an earlier resolution")
        return self


class ClarificationRecord(StrictModel):
    job_id: JobId
    recorded_by: Name
    recorded_at: UtcDateTime
    clarification: ClarificationInput
