"""Provenance contract for a transcript-boundary recovery fork."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import JobId, Sha256


class RecoveryManifest(StrictModel):
    """Bind a fresh child job to immutable pre-semantic parent provenance."""

    schema_version: Literal["1.0"] = "1.0"
    parent_job_id: JobId
    parent_job_fingerprint: Sha256
    parent_assignment_fingerprint: Sha256 | None = None
    parent_final_draft_sha256: Sha256 | None = None
    parent_audit_bundle_fingerprint: Sha256 | None = None
    parent_finding_ids: list[str] = Field(default_factory=list, max_length=100_000)
    recovery_basis: str = Field(min_length=1, max_length=1024)
    child_job_id: JobId
    child_job_fingerprint: Sha256
    child_assignment_fingerprint: Sha256 | None = None
    created_at: UtcDateTime

    @field_validator("parent_finding_ids")
    @classmethod
    def finding_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            len(item) != 8 or not item.startswith("AF") or not item[2:].isdigit() for item in value
        ):
            raise ValueError("recovery finding IDs must be sorted and unique")
        return value

    @field_validator("recovery_basis")
    @classmethod
    def basis_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recovery basis must not be blank")
        return value

    @model_validator(mode="after")
    def parent_and_child_are_distinct(self) -> Self:
        if self.parent_job_id == self.child_job_id:
            raise ValueError("recovery parent and child job IDs must differ")
        return self
