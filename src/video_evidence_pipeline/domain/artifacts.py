"""Artifact and lineage envelopes."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import Sha256, validate_relative_storage_path

ArtifactId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]


class ProducerKind(StrEnum):
    ADAPTER = "adapter"
    TOOL = "tool"


class ProducerIdentity(StrictModel):
    kind: ProducerKind
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)


class ArtifactRef(StrictModel):
    artifact_id: ArtifactId
    artifact_type: str = Field(min_length=1, max_length=128)
    content_sha256: Sha256
    size_bytes: int = Field(ge=1)
    relative_storage_path: str

    @field_validator("relative_storage_path")
    @classmethod
    def storage_path_is_safe(cls, value: str) -> str:
        return validate_relative_storage_path(value)


class ArtifactRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    artifact: ArtifactRef
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    producer: ProducerIdentity
    configuration_sha256: Sha256
    created_at: UtcDateTime
    warnings: list[str] = Field(default_factory=list)
    redacted_error: str | None = Field(default=None, max_length=1024)
