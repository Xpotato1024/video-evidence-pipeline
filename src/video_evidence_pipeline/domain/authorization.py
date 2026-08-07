"""Explicit human authorization contract."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import JobId, Sha256


class AuthorizationRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    job_fingerprint: Sha256
    confirmed_by_user: Literal[True]
    authorization_basis: str = Field(min_length=1, max_length=1024)
    allow_local_processing: bool
    allow_external_api: bool = False
    confirmed_at: UtcDateTime
    confirmer_identity: str = Field(min_length=1, max_length=256)
