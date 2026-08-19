"""Job specification contracts and semantic YAML loading."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import JobId
from video_evidence_pipeline.domain.length import CharacterLengthConstraint
from video_evidence_pipeline.domain.locators import (
    canonicalize_box_locator,
    canonicalize_https_locator,
    canonicalize_panopto_locator,
)
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import configuration_error, io_error


class SourceKind(StrEnum):
    LOCAL_FILE = "local_file"
    DIRECT_HTTP = "direct_http"
    PANOPTO = "panopto"
    BOX = "box"
    GENERIC_YTDLP = "generic_ytdlp"


class PrimaryInputKind(StrEnum):
    """The one primary item that becomes a transcript.

    ``source`` remains the v1 acquisition projection.  Keeping it in place
    lets historic video job YAML and its fingerprint remain valid while newer
    jobs opt into an explicit primary kind.
    """

    VIDEO = "video"
    AUDIO = "audio"
    TRANSCRIPT = "transcript"


class SupportingInputKind(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    NOTES = "notes"


class SourceSpec(StrictModel):
    kind: SourceKind
    locator: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def locator_is_canonical_and_stable(self) -> SourceSpec:
        if self.kind is SourceKind.LOCAL_FILE:
            return self
        expected = {
            SourceKind.DIRECT_HTTP: canonicalize_https_locator,
            SourceKind.GENERIC_YTDLP: canonicalize_https_locator,
            SourceKind.PANOPTO: canonicalize_panopto_locator,
            SourceKind.BOX: canonicalize_box_locator,
        }[self.kind](self.locator)
        if expected != self.locator:
            raise ValueError("source locator must use its canonical stable form")
        return self


class PrimaryInputSpec(StrictModel):
    kind: PrimaryInputKind


class SupportingInputSpec(StrictModel):
    kind: SupportingInputKind
    locator: str = Field(min_length=1, max_length=4096)

    @field_validator("locator")
    @classmethod
    def locator_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("supporting input locator must not be blank")
        return value


def canonicalize_source_locator(kind: SourceKind, locator: str) -> str:
    if kind is SourceKind.LOCAL_FILE:
        return locator
    if kind in {SourceKind.DIRECT_HTTP, SourceKind.GENERIC_YTDLP}:
        return canonicalize_https_locator(locator)
    if kind is SourceKind.PANOPTO:
        return canonicalize_panopto_locator(locator)
    if kind is SourceKind.BOX:
        return canonicalize_box_locator(locator)
    raise ValueError("unsupported source kind")


RequirementText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=512,
        pattern=r".*\S.*",
    ),
]


class AssignmentSpec(StrictModel):
    task_type: str = Field(min_length=1, max_length=128)
    language: str = Field(min_length=2, max_length=32)
    requirements: list[RequirementText] = Field(default_factory=list, max_length=100)
    length: CharacterLengthConstraint | None = None


class OutputSpec(StrictModel):
    format: str = Field(min_length=1, max_length=64)


class JobSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    created_at: UtcDateTime
    source: SourceSpec
    primary_input: PrimaryInputSpec | None = None
    supporting_inputs: list[SupportingInputSpec] = Field(default_factory=list, max_length=32)
    assignment: AssignmentSpec
    output: OutputSpec

    @model_validator(mode="after")
    def input_contract_is_bounded(self) -> Self:
        if (
            self.primary_input is not None
            and self.primary_input.kind
            in {
                PrimaryInputKind.AUDIO,
                PrimaryInputKind.TRANSCRIPT,
            }
            and self.source.kind is not SourceKind.LOCAL_FILE
        ):
            raise ValueError("audio and supplied transcript inputs must use a local file source")
        identities = [(item.kind.value, item.locator) for item in self.supporting_inputs]
        if identities != sorted(set(identities)):
            raise ValueError("supporting inputs must be sorted and unique")
        return self


def primary_input_kind(job: JobSpec) -> PrimaryInputKind:
    """Return the v1-compatible primary input projection for ``job``."""

    return PrimaryInputKind.VIDEO if job.primary_input is None else job.primary_input.kind


def requires_transcript_refinement(job: JobSpec) -> bool:
    """New input declarations opt into the refinement stage; v1 jobs do not."""

    return job.primary_input is not None or bool(job.supporting_inputs)


def job_fingerprint(job: JobSpec) -> str:
    # Historic jobs did not serialize primary/supporting input fields.  Keep
    # their authorization and transition bindings byte-for-byte compatible.
    serialized = job.model_dump(mode="json")
    data = {
        key: serialized[key]
        for key in ("schema_version", "job_id", "created_at", "source", "assignment", "output")
    }
    if job.assignment.length is None:
        data["assignment"].pop("length", None)
    if job.primary_input is not None or job.supporting_inputs:
        data["primary_input"] = serialized["primary_input"]
        data["supporting_inputs"] = serialized["supporting_inputs"]
    return fingerprint(data)


def parse_job_data(data: Any) -> JobSpec:
    validation_failed = False
    try:
        job = JobSpec.model_validate(data)
    except ValidationError:
        validation_failed = True
    if validation_failed:
        raise configuration_error("JOB_INVALID", "job specification is invalid")
    return job


def load_job_yaml(path: Path) -> JobSpec:
    error = None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        error = io_error("JOB_NOT_FOUND", "job specification does not exist")
    except (OSError, UnicodeError, yaml.YAMLError):
        error = configuration_error("JOB_YAML_INVALID", "job YAML could not be parsed")
    if error is not None:
        raise error
    return parse_job_data(data)


def job_to_yaml(job: JobSpec) -> str:
    data = job.model_dump(mode="json")
    if job.assignment.length is None:
        data["assignment"].pop("length", None)
    if job.primary_input is None:
        data.pop("primary_input", None)
    if not job.supporting_inputs:
        data.pop("supporting_inputs", None)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
