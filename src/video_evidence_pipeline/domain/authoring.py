"""Strict contracts for evidence-bound authoring, audit, revision, and human review."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StringConstraints, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.evidence import (
    AmbiguityRecord,
    AssignmentRequirement,
    EvidenceManifest,
    EvidenceRecord,
    EvidenceRequestManifest,
    SourceRef,
)
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.length import (
    CharacterLengthCheck,
    CharacterLengthConstraint,
    EffectiveCharacterLengthConstraint,
)
from video_evidence_pipeline.domain.semantic import SkillSnapshot
from video_evidence_pipeline.domain.serialization import fingerprint

NonBlank128 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r".*\S.*"),
]
NonBlank256 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256, pattern=r".*\S.*"),
]
NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=65_536, pattern=r".*\S.*"),
]
ContextId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
DraftVersion = Annotated[str, StringConstraints(pattern=r"^v00[1-3]$")]


class AgentRole(StrEnum):
    AUTHOR = "author"
    AUDITOR = "auditor"
    REVISER = "reviser"


class AgentExecutionMode(StrEnum):
    EXTERNAL_EXCHANGE = "external_exchange"
    TEST_FIXTURE = "test_fixture"


class ClaimType(StrEnum):
    VIDEO_FACT = "video_fact"
    VISUAL_FACT = "visual_fact"
    INFERENCE = "inference"
    USER_OPINION = "user_opinion"
    EXTERNAL_FACT = "external_fact"
    TRANSITION_OR_RHETORIC = "transition_or_rhetoric"


class ClaimConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RequirementCoverage(StrEnum):
    SATISFIED = "satisfied"
    PARTIAL = "partial"
    UNMET = "unmet"


class AuditSeverity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class AuditDisposition(StrEnum):
    HUMAN_REVIEW_READY = "human_review_ready"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class ClaimComparisonStatus(StrEnum):
    MATCHED = "matched"
    CHANGED = "changed"
    AUTHOR_ONLY = "author_only"
    AUDIT_ONLY = "audit_only"


class ResolutionDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


class AgentIdentity(StrictModel):
    role: AgentRole
    provider: NonBlank128
    model: NonBlank256
    version: NonBlank128
    executor: NonBlank128
    context_id: ContextId
    execution_mode: AgentExecutionMode
    external_api_used: StrictBool


class DraftSpan(StrictModel):
    start: int = Field(ge=0, description="Inclusive Python Unicode code-point offset")
    end: int = Field(gt=0, description="Exclusive Python Unicode code-point offset")
    text: str = Field(min_length=1, max_length=65_536)

    @model_validator(mode="after")
    def interval_is_nonempty(self) -> Self:
        if self.end <= self.start:
            raise ValueError("draft span must be non-empty")
        return self


class ClaimRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    claim_id: str = Field(pattern=r"^C[0-9]{6}$")
    draft_span: DraftSpan
    text: str = Field(min_length=1, max_length=65_536)
    claim_type: ClaimType
    evidence_ids: list[str] = Field(default_factory=list, max_length=256)
    requirement_ids: list[str] = Field(default_factory=list, max_length=100)
    author_confidence: ClaimConfidence
    requires_user_confirmation: StrictBool = False

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            len(item) != 7 or not item.startswith("E") or not item[1:].isdigit() for item in value
        ):
            raise ValueError("claim evidence IDs must be sorted and unique")
        return value

    @field_validator("requirement_ids")
    @classmethod
    def requirement_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            len(item) != 7 or not item.startswith("R") or not item[1:].isdigit() for item in value
        ):
            raise ValueError("claim requirement IDs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def claim_is_evidence_safe(self) -> Self:
        if self.text != self.draft_span.text:
            raise ValueError("claim text must equal its exact draft span")
        evidence_bound = {
            ClaimType.VIDEO_FACT,
            ClaimType.VISUAL_FACT,
            ClaimType.INFERENCE,
            ClaimType.EXTERNAL_FACT,
        }
        if self.claim_type in evidence_bound and not self.evidence_ids:
            raise ValueError("material factual claims require evidence")
        if self.claim_type is ClaimType.USER_OPINION:
            if not self.requires_user_confirmation or "[要確認]" not in self.text:
                raise ValueError(
                    "generated user opinion must remain an explicit confirmation marker"
                )
        elif self.requires_user_confirmation:
            raise ValueError("only user-opinion claims may request user confirmation")
        return self


class RequirementMapEntry(StrictModel):
    requirement_id: str = Field(pattern=r"^R[0-9]{6}$")
    requirement_text: str = Field(min_length=1, max_length=512)
    coverage: RequirementCoverage
    claim_ids: list[str] = Field(default_factory=list, max_length=10_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10_000)
    explanation: NonBlankText

    @field_validator("claim_ids")
    @classmethod
    def claim_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            len(item) != 7 or not item.startswith("C") or not item[1:].isdigit() for item in value
        ):
            raise ValueError("requirement claim IDs must be sorted and unique")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            len(item) != 7 or not item.startswith("E") or not item[1:].isdigit() for item in value
        ):
            raise ValueError("requirement evidence IDs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def satisfied_requirements_have_claims(self) -> Self:
        if self.coverage is RequirementCoverage.SATISFIED and not self.claim_ids:
            raise ValueError("satisfied requirement must reference a claim")
        return self


class RequirementMap(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    entries: list[RequirementMapEntry] = Field(max_length=100)

    @model_validator(mode="after")
    def entries_are_contiguous(self) -> Self:
        identifiers = [item.requirement_id for item in self.entries]
        if identifiers != [f"R{index:06d}" for index in range(1, len(identifiers) + 1)]:
            raise ValueError("requirement map IDs must be contiguous")
        return self


def fixed_evidence_identity(
    *,
    job_id: str,
    evidence_request: EvidenceRequestManifest,
    evidence_manifest: EvidenceManifest,
    evidence_records: list[EvidenceRecord],
    ambiguity_records: list[AmbiguityRecord],
) -> str:
    return fingerprint(
        {
            "job_id": job_id,
            "evidence_request": {
                **evidence_request.model_dump(mode="json"),
            },
            "evidence_manifest": evidence_manifest.model_dump(mode="json"),
            "evidence_records": [item.model_dump(mode="json") for item in evidence_records],
            "ambiguity_records": [item.model_dump(mode="json") for item in ambiguity_records],
        }
    )


def fixed_evidence_identity_matches(
    *,
    job_id: str,
    evidence_request: EvidenceRequestManifest,
    evidence_manifest: EvidenceManifest,
    evidence_records: list[EvidenceRecord],
    ambiguity_records: list[AmbiguityRecord],
    stored_fingerprint: str,
) -> bool:
    return stored_fingerprint == fixed_evidence_identity(
        job_id=job_id,
        evidence_request=evidence_request,
        evidence_manifest=evidence_manifest,
        evidence_records=evidence_records,
        ambiguity_records=ambiguity_records,
    )


class FixedEvidenceBundle(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    fixed_evidence_fingerprint: Sha256
    evidence_request: EvidenceRequestManifest
    evidence_manifest: EvidenceManifest
    evidence_records: list[EvidenceRecord] = Field(max_length=100_000)
    ambiguity_records: list[AmbiguityRecord] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def bundle_is_exact(self) -> Self:
        if (
            self.evidence_request.job_id != self.job_id
            or self.evidence_manifest.job_id != self.job_id
        ):
            raise ValueError("fixed evidence bundle belongs to another job")
        if self.evidence_manifest.request_fingerprint != self.evidence_request.request_fingerprint:
            raise ValueError("fixed evidence request binding is inconsistent")
        evidence_ids = [item.evidence_id for item in self.evidence_records]
        if evidence_ids != [f"E{index:06d}" for index in range(1, len(evidence_ids) + 1)]:
            raise ValueError("fixed evidence IDs must be contiguous")
        ambiguity_ids = [item.ambiguity_id for item in self.ambiguity_records]
        if ambiguity_ids != sorted(set(ambiguity_ids)):
            raise ValueError("fixed ambiguity IDs must be sorted and unique")
        if self.evidence_manifest.evidence_count != len(
            self.evidence_records
        ) or self.evidence_manifest.ambiguity_count != len(self.ambiguity_records):
            raise ValueError("fixed evidence counts are inconsistent")
        return self


class OutputConstraints(StrictModel):
    format: NonBlank128
    language: NonBlank128
    task_type: NonBlank128
    requirements: list[AssignmentRequirement] = Field(max_length=100)
    length: CharacterLengthConstraint | None = None
    effective_length: EffectiveCharacterLengthConstraint | None = None
    purpose: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        exclude_if=lambda value: value is None,
    )
    audience: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def requirements_are_contiguous(self) -> Self:
        identifiers = [item.requirement_id for item in self.requirements]
        if identifiers != [f"R{index:06d}" for index in range(1, len(identifiers) + 1)]:
            raise ValueError("output requirements must be contiguous")
        if (self.length is None) != (self.effective_length is None):
            raise ValueError("raw and effective length constraints must be recorded together")
        return self


class AuthoringRequestManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    request_fingerprint: Sha256
    draft_version: Literal["v001"] = "v001"
    revision_count: Literal[0] = 0
    fixed_evidence: FixedEvidenceBundle
    output_constraints: OutputConstraints
    skill: SkillSnapshot
    created_at: UtcDateTime


class AuthoringResponseManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_fingerprint: Sha256
    author: AgentIdentity
    draft_markdown: str = Field(min_length=1, max_length=4 * 1024 * 1024)
    claims: list[ClaimRecord] = Field(min_length=1, max_length=100_000)
    requirement_map: RequirementMap
    created_at: UtcDateTime

    @model_validator(mode="after")
    def response_is_internally_consistent(self) -> Self:
        if self.author.role is not AgentRole.AUTHOR:
            raise ValueError("authoring response requires the author role")
        claim_ids = [item.claim_id for item in self.claims]
        if claim_ids != [f"C{index:06d}" for index in range(1, len(claim_ids) + 1)]:
            raise ValueError("author claims must be contiguous")
        for claim in self.claims:
            if claim.draft_span.end > len(self.draft_markdown) or (
                self.draft_markdown[claim.draft_span.start : claim.draft_span.end]
                != claim.draft_span.text
            ):
                raise ValueError("claim span does not match the Unicode draft text")
        known_claims = set(claim_ids)
        if any(not set(item.claim_ids) <= known_claims for item in self.requirement_map.entries):
            raise ValueError("requirement map references an unknown claim")
        return self


class AgentRunRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    role: AgentRole
    skill: SkillSnapshot
    identity: AgentIdentity
    request_fingerprint: Sha256
    response_fingerprint: Sha256
    input_artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=100_000)
    output_artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=100_000)
    started_at: UtcDateTime
    finished_at: UtcDateTime
    warnings: list[NonBlank256] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def run_is_role_consistent(self) -> Self:
        if self.role is not self.identity.role or self.finished_at < self.started_at:
            raise ValueError("agent run identity or timeline is inconsistent")
        if self.warnings != sorted(set(self.warnings)):
            raise ValueError("agent run warnings must be sorted and unique")
        return self


class DraftManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    draft_version: DraftVersion
    revision_count: int = Field(ge=0, le=2)
    semantic_revision_count: int | None = Field(default=None, ge=0, le=2)
    length_adjustment_count: int = Field(default=0, ge=0, le=10)
    length_check: CharacterLengthCheck | None = None
    predecessor_draft_sha256: Sha256 | None = None
    fixed_evidence_fingerprint: Sha256
    request_fingerprint: Sha256
    response_fingerprint: Sha256
    bundle_fingerprint: Sha256
    producer: AgentIdentity
    draft_artifact: ArtifactRef
    claims_artifact: ArtifactRef
    requirement_map_artifact: ArtifactRef
    agent_run_artifact: ArtifactRef
    resolution_artifact: ArtifactRef | None = None
    created_at: UtcDateTime

    @model_validator(mode="after")
    def version_matches_revision_count(self) -> Self:
        expected = f"v{self.revision_count + 1:03d}"
        if self.draft_version != expected:
            raise ValueError("draft version and revision count are inconsistent")
        if (
            self.semantic_revision_count is not None
            and self.semantic_revision_count != self.revision_count
        ):
            raise ValueError("semantic revision count is inconsistent")
        if self.revision_count == 0 and (
            self.predecessor_draft_sha256 is not None or self.resolution_artifact is not None
        ):
            raise ValueError("initial draft cannot have revision lineage")
        if self.revision_count > 0 and (
            self.predecessor_draft_sha256 is None or self.resolution_artifact is None
        ):
            raise ValueError("revised draft requires complete revision lineage")
        return self


class AuditClaimRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    audit_claim_id: str = Field(pattern=r"^AC[0-9]{6}$")
    draft_span: DraftSpan
    text: str = Field(min_length=1, max_length=65_536)
    claim_type: ClaimType
    evidence_ids: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            len(item) != 7 or not item.startswith("E") or not item[1:].isdigit() for item in value
        ):
            raise ValueError("audit claim evidence IDs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def text_matches_span(self) -> Self:
        if self.text != self.draft_span.text:
            raise ValueError("audit claim text must equal its exact draft span")
        if (
            self.claim_type
            in {
                ClaimType.VIDEO_FACT,
                ClaimType.VISUAL_FACT,
                ClaimType.INFERENCE,
                ClaimType.EXTERNAL_FACT,
            }
            and not self.evidence_ids
        ):
            raise ValueError("material audit claims require evidence")
        return self


class ClaimComparisonRecord(StrictModel):
    """Deterministic post-import comparison; never part of an audit request."""

    schema_version: Literal["1.0"] = "1.0"
    comparison_id: str = Field(pattern=r"^CP[0-9]{6}$")
    status: ClaimComparisonStatus
    draft_span: DraftSpan
    author_claim_id: str | None = Field(default=None, pattern=r"^C[0-9]{6}$")
    audit_claim_id: str | None = Field(default=None, pattern=r"^AC[0-9]{6}$")
    differences: list[Literal["claim_type", "evidence_ids"]] = Field(
        default_factory=list,
        max_length=2,
    )

    @model_validator(mode="after")
    def comparison_shape_matches_status(self) -> Self:
        if self.differences != sorted(set(self.differences)):
            raise ValueError("claim comparison differences must be sorted and unique")
        both = self.author_claim_id is not None and self.audit_claim_id is not None
        if self.status in {ClaimComparisonStatus.MATCHED, ClaimComparisonStatus.CHANGED}:
            if not both:
                raise ValueError("paired claim comparison requires both claim IDs")
        elif self.status is ClaimComparisonStatus.AUTHOR_ONLY:
            if self.author_claim_id is None or self.audit_claim_id is not None:
                raise ValueError("author-only comparison has invalid claim IDs")
        elif self.author_claim_id is not None or self.audit_claim_id is None:
            raise ValueError("audit-only comparison has invalid claim IDs")
        if self.status is ClaimComparisonStatus.CHANGED and not self.differences:
            raise ValueError("changed claim comparison requires differences")
        if self.status is not ClaimComparisonStatus.CHANGED and self.differences:
            raise ValueError("only changed claim comparisons may list differences")
        return self


class AuditFinding(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    finding_id: str = Field(pattern=r"^AF[0-9]{6}$")
    severity: AuditSeverity
    category: NonBlank128
    rationale: NonBlankText
    draft_span: DraftSpan | None = None
    audit_claim_ids: list[str] = Field(default_factory=list, max_length=10_000)
    requirement_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10_000)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=10_000)
    confidence: ClaimConfidence

    @field_validator("audit_claim_ids", "requirement_ids", "evidence_ids")
    @classmethod
    def identifiers_are_sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("finding identifiers must be sorted and unique")
        return value

    @model_validator(mode="after")
    def finding_is_reproducibly_bound(self) -> Self:
        """Keep every finding attached to a reproducible draft-side target.

        Evidence and source references explain a finding, but they do not identify
        what in the submitted draft must be inspected or revised.  Material
        findings therefore always carry an exact draft anchor; lower-severity
        findings may instead be bound to an audit claim or assignment requirement.
        """

        if not (self.draft_span or self.audit_claim_ids or self.requirement_ids):
            raise ValueError("audit finding requires a draft, claim, or requirement binding")
        if self.severity in {AuditSeverity.P0, AuditSeverity.P1} and self.draft_span is None:
            raise ValueError("material audit finding requires an exact draft span")
        keys = [
            (
                item.kind.value,
                item.ref_id,
                item.start_ms,
                item.end_ms,
                item.content_sha256,
            )
            for item in self.source_refs
        ]
        if keys != sorted(set(keys)):
            raise ValueError("finding source references must be sorted and unique")
        if self.evidence_ids and not self.source_refs:
            raise ValueError("evidence-linked finding requires exact source references")
        if self.source_refs and not self.evidence_ids:
            raise ValueError("finding source references require evidence identifiers")
        return self


class AuditRequestManifest(StrictModel):
    """Fresh-context request. Author claims, run identity, and context are absent by design."""

    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    request_fingerprint: Sha256
    target_draft_version: DraftVersion
    target_draft_sha256: Sha256
    target_draft_artifact: ArtifactRef
    target_draft_markdown: str = Field(min_length=1, max_length=4 * 1024 * 1024)
    fixed_evidence: FixedEvidenceBundle
    output_constraints: OutputConstraints
    skill: SkillSnapshot
    created_at: UtcDateTime


class AuditResponseManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_fingerprint: Sha256
    target_draft_sha256: Sha256
    auditor: AgentIdentity
    audit_claims: list[AuditClaimRecord] = Field(min_length=1, max_length=100_000)
    findings: list[AuditFinding] = Field(default_factory=list, max_length=100_000)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def response_is_internally_consistent(self) -> Self:
        if self.auditor.role is not AgentRole.AUDITOR:
            raise ValueError("audit response requires the auditor role")
        claim_ids = [item.audit_claim_id for item in self.audit_claims]
        if claim_ids != [f"AC{index:06d}" for index in range(1, len(claim_ids) + 1)]:
            raise ValueError("audit claims must be contiguous")
        finding_ids = [item.finding_id for item in self.findings]
        if finding_ids != [f"AF{index:06d}" for index in range(1, len(finding_ids) + 1)]:
            raise ValueError("audit findings must be contiguous")
        return self


class AuditManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    target_draft_version: DraftVersion
    target_draft_sha256: Sha256
    fixed_evidence_fingerprint: Sha256
    request_fingerprint: Sha256
    response_fingerprint: Sha256
    bundle_fingerprint: Sha256
    authoring_context_ids: list[ContextId] = Field(min_length=1, max_length=3)
    prior_auditor_context_ids: list[ContextId] = Field(default_factory=list, max_length=2)
    auditor: AgentIdentity
    audit_claims_artifact: ArtifactRef
    claim_comparison_artifact: ArtifactRef
    findings_artifact: ArtifactRef
    response_artifact: ArtifactRef
    agent_run_artifact: ArtifactRef
    p0_count: int = Field(ge=0)
    p1_count: int = Field(ge=0)
    p2_count: int = Field(ge=0)
    claim_comparison_count: int = Field(ge=1, le=200_000)
    disposition: AuditDisposition
    created_at: UtcDateTime

    @model_validator(mode="after")
    def disposition_matches_findings(self) -> Self:
        if (
            self.authoring_context_ids != sorted(set(self.authoring_context_ids))
            or self.prior_auditor_context_ids != sorted(set(self.prior_auditor_context_ids))
            or set(self.authoring_context_ids) & set(self.prior_auditor_context_ids)
        ):
            raise ValueError("audit context histories must be canonical and disjoint")
        if self.auditor.context_id in {
            *self.authoring_context_ids,
            *self.prior_auditor_context_ids,
        }:
            raise ValueError("auditor context must be fresh for the target draft")
        material = self.p0_count + self.p1_count
        if material == 0 and self.disposition is not AuditDisposition.HUMAN_REVIEW_READY:
            raise ValueError("clean audit must proceed to human review")
        if material > 0 and self.disposition is AuditDisposition.HUMAN_REVIEW_READY:
            raise ValueError("material findings cannot proceed to human review")
        return self


class ResolutionRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    finding_id: str = Field(pattern=r"^AF[0-9]{6}$")
    decision: ResolutionDecision
    resolution: NonBlankText
    changed_spans: list[DraftSpan] = Field(default_factory=list, max_length=10_000)
    removed_spans: list[DraftSpan] = Field(default_factory=list, max_length=10_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10_000)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("resolution evidence IDs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def accepted_resolution_records_changes(self) -> Self:
        if (
            self.decision is ResolutionDecision.ACCEPT
            and not self.changed_spans
            and not self.removed_spans
        ):
            raise ValueError("accepted finding must record changed or removed draft spans")
        if self.decision is not ResolutionDecision.ACCEPT and (
            self.changed_spans or self.removed_spans
        ):
            raise ValueError("rejected or deferred finding cannot claim a draft change")
        return self


class RevisionRequestManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    request_fingerprint: Sha256
    target_draft_version: DraftVersion
    next_draft_version: DraftVersion
    revision_count: int = Field(ge=1, le=2)
    target_draft_sha256: Sha256
    target_draft_artifact: ArtifactRef
    target_draft_markdown: str = Field(min_length=1, max_length=4 * 1024 * 1024)
    author_claims: list[ClaimRecord] = Field(min_length=1, max_length=100_000)
    requirement_map: RequirementMap
    audit_bundle_fingerprint: Sha256
    audit_finding_artifact: ArtifactRef
    audit_findings: list[AuditFinding] = Field(min_length=1, max_length=100_000)
    fixed_evidence: FixedEvidenceBundle
    output_constraints: OutputConstraints
    skill: SkillSnapshot
    created_at: UtcDateTime

    @model_validator(mode="after")
    def revision_versions_are_ordered(self) -> Self:
        if self.next_draft_version != f"v{self.revision_count + 1:03d}":
            raise ValueError("next draft version is inconsistent")
        if self.target_draft_version != f"v{self.revision_count:03d}":
            raise ValueError("revision target version is inconsistent")
        return self


class RevisionResponseManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_fingerprint: Sha256
    reviser: AgentIdentity
    draft_markdown: str = Field(min_length=1, max_length=4 * 1024 * 1024)
    claims: list[ClaimRecord] = Field(min_length=1, max_length=100_000)
    requirement_map: RequirementMap
    resolutions: list[ResolutionRecord] = Field(min_length=1, max_length=100_000)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def response_is_internally_consistent(self) -> Self:
        if self.reviser.role is not AgentRole.REVISER:
            raise ValueError("revision response requires the reviser role")
        claim_ids = [item.claim_id for item in self.claims]
        if claim_ids != [f"C{index:06d}" for index in range(1, len(claim_ids) + 1)]:
            raise ValueError("revised claims must be contiguous")
        resolution_ids = [item.finding_id for item in self.resolutions]
        if resolution_ids != sorted(set(resolution_ids)):
            raise ValueError("resolution finding IDs must be sorted and unique")
        for claim in self.claims:
            if claim.draft_span.end > len(self.draft_markdown) or (
                self.draft_markdown[claim.draft_span.start : claim.draft_span.end]
                != claim.draft_span.text
            ):
                raise ValueError("revised claim span does not match the Unicode draft text")
        return self


class RevisionManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    target_draft_version: DraftVersion
    revised_draft_version: DraftVersion
    target_draft_sha256: Sha256
    audit_bundle_fingerprint: Sha256
    request_fingerprint: Sha256
    response_fingerprint: Sha256
    resolution_artifact: ArtifactRef
    prior_context_ids: list[ContextId] = Field(min_length=2, max_length=6)
    bundle_fingerprint: Sha256
    created_at: UtcDateTime

    @model_validator(mode="after")
    def draft_versions_are_adjacent(self) -> Self:
        target = int(self.target_draft_version[1:])
        revised = int(self.revised_draft_version[1:])
        if revised != target + 1:
            raise ValueError("revision manifest draft versions must be adjacent")
        if self.prior_context_ids != sorted(set(self.prior_context_ids)):
            raise ValueError("revision prior context IDs must be sorted and unique")
        return self


class FormatCheck(StrictModel):
    check_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    passed: StrictBool
    detail: NonBlankText


class RevisionHistoryEntry(StrictModel):
    draft_version: DraftVersion
    draft_sha256: Sha256
    audit_bundle_fingerprint: Sha256 | None = None
    resolution_artifact: ArtifactRef | None = None


class HumanReviewBundle(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    fixed_evidence_fingerprint: Sha256
    current_draft_version: DraftVersion
    current_draft_artifact: ArtifactRef
    current_draft_sha256: Sha256
    audit_bundle_fingerprint: Sha256
    audit_findings_artifact: ArtifactRef
    claim_comparison_artifact: ArtifactRef
    p0_count: Literal[0] = 0
    p1_count: Literal[0] = 0
    p2_count: int = Field(ge=0)
    unresolved_ambiguity_ids: list[str] = Field(default_factory=list, max_length=100_000)
    confirmation_spans: list[DraftSpan] = Field(default_factory=list, max_length=100_000)
    user_opinion_claim_ids: list[str] = Field(default_factory=list, max_length=100_000)
    external_fact_claim_ids: list[str] = Field(default_factory=list, max_length=100_000)
    replay_source_refs: list[SourceRef] = Field(default_factory=list, max_length=100_000)
    requirement_map: RequirementMap
    format_checks: list[FormatCheck] = Field(min_length=1, max_length=1000)
    length_check: CharacterLengthCheck | None = None
    semantic_revision_count: int | None = Field(default=None, ge=0, le=2)
    length_adjustment_count: int | None = Field(default=None, ge=0, le=10)
    revision_history: list[RevisionHistoryEntry] = Field(min_length=1, max_length=3)
    approval_target_sha256: Sha256
    is_approval: Literal[False] = False
    created_at: UtcDateTime

    @model_validator(mode="after")
    def bundle_is_review_only(self) -> Self:
        historical_fields = {
            "length_check",
            "semantic_revision_count",
            "length_adjustment_count",
        }
        present_historical_fields = historical_fields & self.model_fields_set
        if present_historical_fields and present_historical_fields != historical_fields:
            raise ValueError(
                "human review length metadata fields must be all present or all absent"
            )
        if not (
            self.current_draft_artifact.content_sha256
            == self.current_draft_sha256
            == self.approval_target_sha256
        ):
            raise ValueError("human review approval target is stale")
        for values in (
            self.unresolved_ambiguity_ids,
            self.user_opinion_claim_ids,
            self.external_fact_claim_ids,
        ):
            if values != sorted(set(values)):
                raise ValueError("human review identifiers must be sorted and unique")
        if [item.draft_version for item in self.revision_history] != [
            f"v{index:03d}" for index in range(1, len(self.revision_history) + 1)
        ]:
            raise ValueError("human review revision history is not contiguous")
        return self


class HumanReviewManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    fixed_evidence_fingerprint: Sha256
    draft_bundle_fingerprint: Sha256
    audit_bundle_fingerprint: Sha256
    bundle_fingerprint: Sha256
    bundle_artifact: ArtifactRef
    approval_target_sha256: Sha256
    created_at: UtcDateTime
