from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.authoring import (
    AgentExecutionMode,
    AgentIdentity,
    AgentRole,
    AuditClaimRecord,
    AuditDisposition,
    AuditFinding,
    AuditManifest,
    AuditSeverity,
    AuthoringResponseManifest,
    ClaimComparisonRecord,
    ClaimComparisonStatus,
    ClaimConfidence,
    ClaimRecord,
    ClaimType,
    DraftSpan,
    FixedEvidenceBundle,
    FormatCheck,
    HumanReviewBundle,
    RequirementMap,
    ResolutionDecision,
    ResolutionRecord,
    RevisionHistoryEntry,
    RevisionManifest,
)

SHA = "1" * 64


def _artifact(name: str = "artifact") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        artifact_type=name,
        content_sha256=SHA,
        size_bytes=1,
        relative_storage_path=f"authoring/{name}.json",
    )


def _identity(role: AgentRole, context: str) -> AgentIdentity:
    return AgentIdentity(
        role=role,
        provider="provider",
        model="model",
        version="1",
        executor="executor",
        context_id=context,
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        external_api_used=False,
    )


def _fixed_evidence() -> FixedEvidenceBundle:
    # These tests exercise envelopes that do not inspect the upstream catalogs. Full
    # FixedEvidenceBundle construction and verification is covered by pipeline E2E tests.
    return FixedEvidenceBundle.model_construct(
        job_id="authoring-domain",
        fixed_evidence_fingerprint=SHA,
        evidence_request=None,
        evidence_manifest=None,
        evidence_records=[],
        ambiguity_records=[],
    )


def test_unicode_draft_span_is_exact_code_point_slice() -> None:
    draft = "制御🙂は安定です。"
    text = "🙂は安定"
    start = draft.index(text)
    span = DraftSpan(start=start, end=start + len(text), text=text)
    claim = ClaimRecord(
        claim_id="C000001",
        draft_span=span,
        text=text,
        claim_type=ClaimType.VIDEO_FACT,
        evidence_ids=["E000001"],
        requirement_ids=[],
        author_confidence=ClaimConfidence.HIGH,
    )
    response = AuthoringResponseManifest(
        request_fingerprint=SHA,
        author=_identity(AgentRole.AUTHOR, "author-context"),
        draft_markdown=draft,
        claims=[claim],
        requirement_map=RequirementMap(entries=[]),
        created_at=datetime.now(UTC),
    )
    assert response.claims[0].draft_span.text == text

    with pytest.raises(ValidationError):
        response.model_copy(
            update={
                "claims": [
                    claim.model_copy(
                        update={"draft_span": span.model_copy(update={"start": start + 1})}
                    )
                ]
            }
        ).__class__.model_validate(
            response.model_copy(
                update={
                    "claims": [
                        claim.model_copy(
                            update={"draft_span": span.model_copy(update={"start": start + 1})}
                        )
                    ]
                }
            ).model_dump(),
            strict=True,
        )


def test_material_claim_requires_evidence_and_user_opinion_remains_marker() -> None:
    with pytest.raises(ValidationError):
        ClaimRecord(
            claim_id="C000001",
            draft_span=DraftSpan(start=0, end=4, text="事実です"),
            text="事実です",
            claim_type=ClaimType.VIDEO_FACT,
            evidence_ids=[],
            requirement_ids=[],
            author_confidence=ClaimConfidence.LOW,
        )
    with pytest.raises(ValidationError):
        ClaimRecord(
            claim_id="C000001",
            draft_span=DraftSpan(start=0, end=3, text="私は思う"),
            text="私は思う",
            claim_type=ClaimType.USER_OPINION,
            evidence_ids=[],
            requirement_ids=[],
            author_confidence=ClaimConfidence.LOW,
            requires_user_confirmation=True,
        )

    marker = "[要確認] 私の意見"
    claim = ClaimRecord(
        claim_id="C000001",
        draft_span=DraftSpan(start=0, end=len(marker), text=marker),
        text=marker,
        claim_type=ClaimType.USER_OPINION,
        evidence_ids=[],
        requirement_ids=[],
        author_confidence=ClaimConfidence.LOW,
        requires_user_confirmation=True,
    )
    assert claim.requires_user_confirmation is True

    with pytest.raises(ValidationError):
        AuditClaimRecord(
            audit_claim_id="AC000001",
            draft_span=DraftSpan(start=0, end=4, text="fact"),
            text="fact",
            claim_type=ClaimType.VIDEO_FACT,
            evidence_ids=[],
        )


def test_audit_finding_requires_reproducible_target_and_material_draft_anchor() -> None:
    common = {
        "finding_id": "AF000001",
        "severity": AuditSeverity.P1,
        "category": "evidence-binding",
        "rationale": "A reproducible target is required.",
        "confidence": ClaimConfidence.HIGH,
    }
    with pytest.raises(ValidationError):
        AuditFinding(**common)
    with pytest.raises(ValidationError):
        AuditFinding(**{**common, "requirement_ids": ["R000001"]})
    finding = AuditFinding(
        **{
            **common,
            "draft_span": DraftSpan(start=0, end=4, text="fact"),
            "requirement_ids": ["R000001"],
        }
    )
    assert finding.draft_span is not None


def test_audit_manifest_rejects_same_or_materially_clean_context_decision() -> None:
    common = {
        "job_id": "authoring-domain",
        "target_draft_version": "v001",
        "target_draft_sha256": SHA,
        "fixed_evidence_fingerprint": SHA,
        "request_fingerprint": SHA,
        "response_fingerprint": SHA,
        "bundle_fingerprint": SHA,
        "authoring_context_ids": ["author-context"],
        "auditor": _identity(AgentRole.AUDITOR, "audit-context"),
        "audit_claims_artifact": _artifact("audit-claims"),
        "claim_comparison_artifact": _artifact("claim-comparison"),
        "findings_artifact": _artifact("audit-findings"),
        "response_artifact": _artifact("audit-response"),
        "agent_run_artifact": _artifact("audit-run"),
        "p0_count": 0,
        "p1_count": 0,
        "p2_count": 1,
        "claim_comparison_count": 1,
        "disposition": AuditDisposition.HUMAN_REVIEW_READY,
        "created_at": datetime.now(UTC),
    }
    assert AuditManifest(**common).p2_count == 1
    with pytest.raises(ValidationError):
        AuditManifest(**{**common, "auditor": _identity(AgentRole.AUDITOR, "author-context")})
    with pytest.raises(ValidationError):
        AuditManifest(**{**common, "disposition": AuditDisposition.REVISION_REQUIRED})
    with pytest.raises(ValidationError):
        AuditManifest(
            **{
                **common,
                "p1_count": 1,
                "disposition": AuditDisposition.HUMAN_REVIEW_READY,
            }
        )


def test_audit_request_schema_has_no_author_context_or_claim_surface() -> None:
    from video_evidence_pipeline.domain.authoring import AuditRequestManifest

    schema_text = json.dumps(AuditRequestManifest.model_json_schema(), sort_keys=True)
    for forbidden in (
        "author_claims",
        "author_context",
        "author_run",
        "private_reasoning",
        "prompt_history",
    ):
        assert forbidden not in schema_text
    assert "fixed_evidence" in schema_text
    assert "target_draft_markdown" in schema_text


def test_revision_resolution_and_human_review_are_bounded_and_not_approval() -> None:
    with pytest.raises(ValidationError):
        ResolutionRecord(
            finding_id="AF000001",
            decision=ResolutionDecision.ACCEPT,
            resolution="修正した",
            changed_spans=[],
            evidence_ids=["E000001"],
        )
    deletion = ResolutionRecord(
        finding_id="AF000001",
        decision=ResolutionDecision.ACCEPT,
        resolution="根拠のない文を削除した",
        removed_spans=[DraftSpan(start=0, end=4, text="fact")],
        evidence_ids=["E000001"],
    )
    assert deletion.changed_spans == []

    bundle = HumanReviewBundle(
        job_id="authoring-domain",
        fixed_evidence_fingerprint=SHA,
        current_draft_version="v003",
        current_draft_artifact=_artifact("draft"),
        current_draft_sha256=SHA,
        audit_bundle_fingerprint=SHA,
        audit_findings_artifact=_artifact("audit-findings"),
        claim_comparison_artifact=_artifact("claim-comparison"),
        p0_count=0,
        p1_count=0,
        p2_count=1,
        unresolved_ambiguity_ids=[],
        confirmation_spans=[],
        user_opinion_claim_ids=[],
        external_fact_claim_ids=[],
        replay_source_refs=[],
        requirement_map=RequirementMap(entries=[]),
        format_checks=[FormatCheck(check_id="markdown.nonempty", passed=True, detail="ok")],
        revision_history=[
            RevisionHistoryEntry(draft_version="v001", draft_sha256=SHA),
            RevisionHistoryEntry(draft_version="v002", draft_sha256=SHA),
            RevisionHistoryEntry(draft_version="v003", draft_sha256=SHA),
        ],
        approval_target_sha256=SHA,
        is_approval=False,
        created_at=datetime.now(UTC),
    )
    assert bundle.is_approval is False
    with pytest.raises(ValidationError):
        HumanReviewBundle.model_validate(
            {**bundle.model_dump(mode="json"), "approval_target_sha256": "2" * 64},
            strict=True,
        )


def test_comparison_and_revision_lineage_contracts_are_strict() -> None:
    comparison = ClaimComparisonRecord(
        comparison_id="CP000001",
        status=ClaimComparisonStatus.MATCHED,
        draft_span=DraftSpan(start=0, end=4, text="fact"),
        author_claim_id="C000001",
        audit_claim_id="AC000001",
    )
    assert comparison.differences == []
    with pytest.raises(ValidationError):
        ClaimComparisonRecord(
            comparison_id="CP000001",
            status=ClaimComparisonStatus.CHANGED,
            draft_span=DraftSpan(start=0, end=4, text="fact"),
            author_claim_id="C000001",
            audit_claim_id="AC000001",
            differences=[],
        )
    with pytest.raises(ValidationError):
        RevisionManifest(
            job_id="authoring-domain",
            target_draft_version="v001",
            revised_draft_version="v003",
            target_draft_sha256=SHA,
            audit_bundle_fingerprint=SHA,
            request_fingerprint=SHA,
            response_fingerprint=SHA,
            resolution_artifact=_artifact("resolution"),
            prior_context_ids=["audit-context", "author-context"],
            bundle_fingerprint=SHA,
            created_at=datetime.now(UTC),
        )
