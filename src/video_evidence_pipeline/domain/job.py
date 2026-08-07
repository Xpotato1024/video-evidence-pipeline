"""Job specification contracts and semantic YAML loading."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, StringConstraints, ValidationError, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import JobId
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import configuration_error, io_error


class SourceKind(StrEnum):
    LOCAL_FILE = "local_file"
    DIRECT_HTTP = "direct_http"
    PANOPTO = "panopto"
    BOX = "box"
    GENERIC_YTDLP = "generic_ytdlp"


class SourceSpec(StrictModel):
    kind: SourceKind
    locator: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def locator_does_not_persist_credentials(self) -> SourceSpec:
        if self.kind is SourceKind.LOCAL_FILE:
            return self
        parsed = urlsplit(self.locator)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source locator must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("source locator query and fragment are not supported")
        return self


RequirementText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]


class AssignmentSpec(StrictModel):
    task_type: str = Field(min_length=1, max_length=128)
    language: str = Field(min_length=2, max_length=32)
    requirements: list[RequirementText] = Field(default_factory=list, max_length=100)


class OutputSpec(StrictModel):
    format: str = Field(min_length=1, max_length=64)


class JobSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    created_at: UtcDateTime
    source: SourceSpec
    assignment: AssignmentSpec
    output: OutputSpec


def job_fingerprint(job: JobSpec) -> str:
    return fingerprint(job)


def parse_job_data(data: Any) -> JobSpec:
    try:
        return JobSpec.model_validate(data)
    except ValidationError as exc:
        raise configuration_error("JOB_INVALID", "job specification is invalid") from exc


def load_job_yaml(path: Path) -> JobSpec:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise io_error("JOB_NOT_FOUND", "job specification does not exist") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise configuration_error("JOB_YAML_INVALID", "job YAML could not be parsed") from exc
    return parse_job_data(data)


def job_to_yaml(job: JobSpec) -> str:
    data = job.model_dump(mode="json")
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
