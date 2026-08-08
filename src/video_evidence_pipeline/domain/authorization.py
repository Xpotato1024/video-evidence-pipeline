"""Explicit human authorization contract."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, field_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import JobId, Sha256


class AuthorizationRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    job_fingerprint: Sha256
    confirmed_by_user: StrictBool = Field(json_schema_extra={"const": True})
    authorization_basis: str = Field(min_length=1, max_length=1024)
    allow_local_processing: StrictBool
    allow_external_api: StrictBool = False
    confirmed_at: UtcDateTime
    confirmer_identity: str = Field(min_length=1, max_length=256)

    @field_validator("confirmed_by_user")
    @classmethod
    def confirmation_must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("confirmed_by_user must be true")
        return value

    @field_validator("authorization_basis", "confirmer_identity")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("authorization text fields must not be blank")
        return value
