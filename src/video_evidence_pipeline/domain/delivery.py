"""Strict approval and byte-exact export contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.serialization import fingerprint

NonBlankIdentity = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
NonBlankBasis = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
NonBlankNote = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]


class ReviewerIdentity(StrictModel):
    """A local CLI assertion, not an authenticated or signed identity."""

    identity: NonBlankIdentity
    assertion_method: Literal["cli_asserted"] = "cli_asserted"
    cryptographically_verified: Literal[False] = False


def _approval_intent_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        key: values[key]
        for key in (
            "job_id",
            "job_fingerprint",
            "human_review_bundle_fingerprint",
            "human_review_artifact",
            "current_audit_bundle_fingerprint",
            "current_audit_findings_artifact",
            "approved_draft_version",
            "approved_draft_artifact",
            "approval_target_sha256",
            "confirmed_by_user",
            "reviewer",
            "basis",
            "unresolved_notes",
        )
    }


def approval_intent_fingerprint(values: dict[str, object]) -> str:
    return fingerprint(_approval_intent_payload(values))


def approval_record_fingerprint(values: dict[str, object]) -> str:
    return fingerprint({key: value for key, value in values.items() if key != "record_sha256"})


class ApprovalRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=1, le=1_000_000)
    approval_id: str = Field(pattern=r"^approval-[0-9a-f]{16}$")
    job_id: JobId
    job_fingerprint: Sha256
    human_review_bundle_fingerprint: Sha256
    human_review_artifact: ArtifactRef
    current_audit_bundle_fingerprint: Sha256
    current_audit_findings_artifact: ArtifactRef
    approved_draft_version: str = Field(pattern=r"^v[0-9]{3}$")
    approved_draft_artifact: ArtifactRef
    approval_target_sha256: Sha256
    confirmed_by_user: Literal[True] = True
    reviewer: ReviewerIdentity
    basis: NonBlankBasis
    unresolved_notes: list[NonBlankNote] = Field(default_factory=list, max_length=1000)
    approved_at: UtcDateTime
    intent_fingerprint: Sha256
    previous_record_sha256: Sha256 | None = None
    record_sha256: Sha256

    @field_validator("unresolved_notes")
    @classmethod
    def notes_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("unresolved notes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def hashes_and_chain_are_consistent(self) -> Self:
        values = self.model_dump(mode="json")
        expected_intent = approval_intent_fingerprint(values)
        if self.intent_fingerprint != expected_intent:
            raise ValueError("approval intent fingerprint is inconsistent")
        if self.approval_id != f"approval-{expected_intent[:16]}":
            raise ValueError("approval ID is inconsistent")
        if self.sequence == 1 and self.previous_record_sha256 is not None:
            raise ValueError("first approval record cannot have a predecessor")
        if self.sequence > 1 and self.previous_record_sha256 is None:
            raise ValueError("later approval record requires a predecessor")
        if self.approval_target_sha256 != self.approved_draft_artifact.content_sha256:
            raise ValueError("approval target does not match the approved draft")
        if self.record_sha256 != approval_record_fingerprint(values):
            raise ValueError("approval record fingerprint is inconsistent")
        return self


def approval_manifest_fingerprint(values: dict[str, object]) -> str:
    return fingerprint(
        {key: value for key, value in values.items() if key != "manifest_fingerprint"}
    )


class ApprovalManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    job_fingerprint: Sha256
    approval_id: str = Field(pattern=r"^approval-[0-9a-f]{16}$")
    approval_record_sequence: int = Field(ge=1)
    approval_record_sha256: Sha256
    approval_record_artifact: ArtifactRef
    approval_intent_fingerprint: Sha256
    human_review_bundle_fingerprint: Sha256
    human_review_artifact: ArtifactRef
    current_audit_bundle_fingerprint: Sha256
    current_audit_findings_artifact: ArtifactRef
    approved_draft_version: str = Field(pattern=r"^v[0-9]{3}$")
    approved_draft_artifact: ArtifactRef
    approval_target_sha256: Sha256
    created_at: UtcDateTime
    manifest_fingerprint: Sha256

    @model_validator(mode="after")
    def fingerprint_is_consistent(self) -> Self:
        values = self.model_dump(mode="json")
        if self.manifest_fingerprint != approval_manifest_fingerprint(values):
            raise ValueError("approval manifest fingerprint is inconsistent")
        if self.approval_target_sha256 != self.approved_draft_artifact.content_sha256:
            raise ValueError("approval manifest target is inconsistent")
        return self


class ExportAdapterIdentity(StrictModel):
    kind: Literal["adapter"] = "adapter"
    name: Literal["markdown-byte-exact"] = "markdown-byte-exact"
    version: str = Field(min_length=1, max_length=128)


def export_package_fingerprint(values: dict[str, object]) -> str:
    return fingerprint(
        {key: value for key, value in values.items() if key != "package_fingerprint"}
    )


class ExportPackageManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    job_fingerprint: Sha256
    approval_record_sha256: Sha256
    approval_record_artifact: ArtifactRef
    approved_draft_version: str = Field(pattern=r"^v[0-9]{3}$")
    approved_draft_artifact: ArtifactRef
    human_review_bundle_fingerprint: Sha256
    human_review_artifact: ArtifactRef
    audit_bundle_fingerprint: Sha256
    fixed_evidence_fingerprint: Sha256
    final_artifact: ArtifactRef
    format: Literal["markdown"] = "markdown"
    adapter: ExportAdapterIdentity
    byte_exact: Literal[True] = True
    created_at: UtcDateTime
    package_fingerprint: Sha256

    @model_validator(mode="after")
    def package_is_consistent(self) -> Self:
        values = self.model_dump(mode="json")
        if self.package_fingerprint != export_package_fingerprint(values):
            raise ValueError("export package fingerprint is inconsistent")
        if (
            self.final_artifact.content_sha256 != self.approved_draft_artifact.content_sha256
            or self.final_artifact.size_bytes != self.approved_draft_artifact.size_bytes
        ):
            raise ValueError("byte-exact export does not match its approved draft")
        return self


def export_manifest_fingerprint(values: dict[str, object]) -> str:
    return fingerprint({key: value for key, value in values.items() if key != "export_fingerprint"})


class ExportManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    job_fingerprint: Sha256
    approval_record_sha256: Sha256
    approval_record_artifact: ArtifactRef
    approved_draft_version: str = Field(pattern=r"^v[0-9]{3}$")
    approved_draft_artifact: ArtifactRef
    human_review_bundle_fingerprint: Sha256
    human_review_artifact: ArtifactRef
    audit_bundle_fingerprint: Sha256
    fixed_evidence_fingerprint: Sha256
    final_artifact: ArtifactRef
    package_manifest_artifact: ArtifactRef
    package_fingerprint: Sha256
    format: Literal["markdown"] = "markdown"
    adapter: ExportAdapterIdentity
    byte_exact: Literal[True] = True
    created_at: UtcDateTime
    export_fingerprint: Sha256

    @model_validator(mode="after")
    def export_is_consistent(self) -> Self:
        values = self.model_dump(mode="json")
        if self.export_fingerprint != export_manifest_fingerprint(values):
            raise ValueError("export manifest fingerprint is inconsistent")
        if (
            self.final_artifact.content_sha256 != self.approved_draft_artifact.content_sha256
            or self.final_artifact.size_bytes != self.approved_draft_artifact.size_bytes
        ):
            raise ValueError("export manifest draft binding is inconsistent")
        return self
