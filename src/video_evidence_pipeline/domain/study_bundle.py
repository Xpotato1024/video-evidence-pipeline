"""Portable, non-runtime study archive contracts."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.evidence import SourceRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.serialization import fingerprint


def portable_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or "\\" in value
        or any(c in value for c in ':*?<>|"')
        or any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in parts)
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("archive paths must be portable relative paths")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    }
    if any(p.split(".")[0].upper() in reserved for p in parts):
        raise ValueError("archive path contains a reserved Windows name")
    return value


class StudyBundleFile(StrictModel):
    relative_path: str = Field(min_length=1, max_length=2048)
    sha256: Sha256
    size_bytes: int = Field(ge=0, le=8 * 1024**4)

    @field_validator("relative_path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return portable_path(value)


def study_bundle_fingerprint(value: dict[str, object]) -> str:
    return fingerprint({k: v for k, v in value.items() if k != "bundle_fingerprint"})


class StudyBundleManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    format: Literal["study-bundle"] = "study-bundle"
    job_id: JobId
    source_job_fingerprint: Sha256
    vep_version: str = Field(min_length=1, max_length=128)
    approval_record_sha256: Sha256
    approved_draft_sha256: Sha256
    approved_at: UtcDateTime
    approval_identity_cryptographically_verified: Literal[False] = False
    input_media_included: Literal[False] = False
    runtime_replay_supported: Literal[False] = False
    entrypoints: dict[str, str]
    files: list[StudyBundleFile] = Field(min_length=1, max_length=10000)
    omitted_source_artifacts: list[StudyBundleFile] = Field(default_factory=list, max_length=10000)
    bundle_fingerprint: Sha256

    @model_validator(mode="after")
    def consistent_inventory(self) -> Self:
        paths = [f.relative_path for f in self.files]
        if paths != sorted(set(paths)) or len({p.casefold() for p in paths}) != len(paths):
            raise ValueError("archive paths must be sorted, unique and case-portable")
        if "bundle-manifest.json" in paths:
            raise ValueError("manifest cannot list itself")
        if (
            any(f.size_bytes > 512 * 1024 * 1024 for f in self.files)
            or sum(f.size_bytes for f in self.files) > 2 * 1024 * 1024 * 1024
        ):
            raise ValueError("archive exceeds its total size limit")
        if (
            not {
                "summary",
                "transcript_raw",
                "evidence",
                "ambiguities",
                "human_review",
                "approval",
                "source_index",
            }
            <= self.entrypoints.keys()
        ):
            raise ValueError("archive entrypoints are incomplete")
        if any(portable_path(p) not in paths for p in self.entrypoints.values()):
            raise ValueError("entrypoint is not in the archive")
        if (
            next(f.sha256 for f in self.files if f.relative_path == self.entrypoints["summary"])
            != self.approved_draft_sha256
        ):
            raise ValueError("summary is not byte-exact approved content")
        omitted = [f.relative_path for f in self.omitted_source_artifacts]
        if omitted != sorted(set(omitted)) or set(omitted) & set(paths):
            raise ValueError("omitted source inventory is inconsistent")
        if any(
            not p.startswith(("source/raw/", "normalized/analysis/", "normalized/audio/"))
            for p in omitted
        ):
            raise ValueError("only input source and normalized A/V may be omitted")
        if self.bundle_fingerprint != study_bundle_fingerprint(self.model_dump(mode="json")):
            raise ValueError("archive manifest fingerprint does not match")
        return self


class StudySourceBinding(StrictModel):
    source_ref: SourceRef
    relative_path: str = Field(min_length=1, max_length=2048)

    @field_validator("relative_path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return portable_path(value)


class StudySourceIndex(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    bindings: list[StudySourceBinding] = Field(default_factory=list, max_length=200000)

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        identities = [fingerprint(item.source_ref) for item in self.bindings]
        if identities != sorted(set(identities)):
            raise ValueError("source bindings must be sorted and unique")
        return self
