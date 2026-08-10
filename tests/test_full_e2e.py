from __future__ import annotations

from pathlib import Path

import pytest
from test_authoring_pipeline import (
    _audit_response,
    _author_response,
    _evidence_ready_job,
    _revision_response,
    _write_model,
)

import video_evidence_pipeline.application as application_module
from video_evidence_pipeline.application import (
    _import_audit_fixture,
    _import_authoring_fixture,
    _import_revision_fixture,
    approve_job,
    build_search_index_job,
    export_job,
    job_status,
    prepare_audit_job,
    prepare_authoring_job,
    prepare_revision_job,
    search_job,
    validate_job,
)
from video_evidence_pipeline.domain.authoring import (
    ClaimConfidence,
    ClaimRecord,
    ClaimType,
    DraftSpan,
)
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.jsonl import read_jsonl


def _publish_author(tmp_path: Path, workspace, job_id: str):
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    response_path = tmp_path / f"{job_id}-author.json"
    _write_model(response_path, _author_response(request, context_id=f"{job_id}-author"))
    return _import_authoring_fixture(workspace.root, response_path)


def _publish_author_with_user_opinion_marker(tmp_path: Path, workspace, job_id: str):
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    response = _author_response(request, context_id=f"{job_id}-author")
    marker = "[要確認] reviewer input is required."
    draft_markdown = f"{response.draft_markdown}\n{marker}"
    opinion = ClaimRecord(
        claim_id="C000002",
        draft_span=DraftSpan(
            start=draft_markdown.index(marker),
            end=draft_markdown.index(marker) + len(marker),
            text=marker,
        ),
        text=marker,
        claim_type=ClaimType.USER_OPINION,
        author_confidence=ClaimConfidence.LOW,
        requires_user_confirmation=True,
    )
    response_path = tmp_path / f"{job_id}-author.json"
    _write_model(
        response_path,
        response.model_copy(
            update={
                "draft_markdown": draft_markdown,
                "claims": [*response.claims, opinion],
            }
        ),
    )
    return _import_authoring_fixture(workspace.root, response_path)


def _publish_audit(tmp_path: Path, workspace, *, context_id: str, material: bool):
    request = prepare_audit_job(workspace.root, "independent-content-audit").request
    response_path = tmp_path / f"{context_id}.json"
    _write_model(
        response_path,
        _audit_response(request, context_id=context_id, material=material),
    )
    return _import_audit_fixture(workspace.root, response_path)


def _publish_revision(tmp_path: Path, workspace, *, context_id: str):
    request = prepare_revision_job(workspace.root, "revise-from-audit").request
    response_path = tmp_path / f"{context_id}.json"
    _write_model(response_path, _revision_response(request, context_id=context_id))
    return _import_revision_fixture(workspace.root, response_path)


def test_full_pipeline_approval_rejects_generated_user_opinion_marker(tmp_path: Path) -> None:
    workspace = _evidence_ready_job(tmp_path, "e2e-user-opinion-marker")
    draft = _publish_author_with_user_opinion_marker(
        tmp_path,
        workspace,
        "e2e-user-opinion-marker",
    )
    user_opinion = next(
        claim for claim in draft.claims if claim.claim_type is ClaimType.USER_OPINION
    )
    assert user_opinion.requires_user_confirmation is True
    assert "[要確認]" in user_opinion.text
    assert (
        "[要確認]".encode()
        in workspace.safe_path(draft.manifest.draft_artifact.relative_storage_path).read_bytes()
    )

    audit = _publish_audit(
        tmp_path,
        workspace,
        context_id="e2e-user-opinion-audit",
        material=False,
    )
    assert audit.human_review is not None
    current_workspace, job, _transitions, state = application_module._load_context(workspace.root)
    _fixed, _verified_draft, _verified_audit, human_review = application_module._approval_upstream(
        current_workspace,
        job,
        state,
        stale_code=False,
    )
    assert human_review.bundle.confirmation_spans == [user_opinion.draft_span]
    assert human_review.bundle.user_opinion_claim_ids == [user_opinion.claim_id]

    transitions_before = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert transitions_before[-1].to_state is JobState.HUMAN_REVIEW_READY
    assert not any(record.to_state is JobState.HUMAN_APPROVED for record in transitions_before)
    assert not any(record.to_state is JobState.EXPORTED for record in transitions_before)
    transition_before = workspace.transitions_jsonl.read_bytes()
    staging_before = tuple(sorted(path.name for path in workspace.delivery_staging_root.iterdir()))
    with pytest.raises(VepError) as approval_error:
        approve_job(
            workspace.root,
            approval_target_sha256=audit.human_review.approval_target_sha256,
            reviewer="synthetic-human-reviewer",
            basis="explicit synthetic review",
            confirm=True,
            unresolved_notes=["generic non-blocking note"],
        )
    assert approval_error.value.code == "APPROVAL_CONFIRMATION_UNRESOLVED"
    transitions_after = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert transitions_after[-1].to_state is JobState.HUMAN_REVIEW_READY
    assert not any(record.to_state is JobState.HUMAN_APPROVED for record in transitions_after)
    assert not any(record.to_state is JobState.EXPORTED for record in transitions_after)
    assert workspace.transitions_jsonl.read_bytes() == transition_before
    assert (
        tuple(sorted(path.name for path in workspace.delivery_staging_root.iterdir()))
        == staging_before
    )
    assert not workspace.approval_records_jsonl.exists()
    assert not workspace.approval_stage_manifest_json.exists()
    assert not workspace.approval_manifest_json.exists()
    assert not workspace.export_stage_manifest_json.exists()
    assert not workspace.export_manifest_json.exists()
    assert not list((workspace.root / "exports" / "markdown").iterdir())


@pytest.mark.parametrize("with_revision", [False, True], ids=["straight", "one-revision"])
def test_full_synthetic_pipeline_reaches_byte_exact_export(
    tmp_path: Path,
    with_revision: bool,
) -> None:
    job_id = "e2e-revision" if with_revision else "e2e-straight"
    workspace = _evidence_ready_job(tmp_path, job_id)

    index = build_search_index_job(workspace.root)
    assert index.reused is False
    assert workspace.search_index_sqlite.is_file()
    assert search_job(workspace.root, "application", limit=10).hits

    initial = _publish_author(tmp_path, workspace, job_id)
    initial_path = workspace.safe_path(initial.manifest.draft_artifact.relative_storage_path)
    initial_bytes = initial_path.read_bytes()

    if with_revision:
        first_audit = _publish_audit(
            tmp_path,
            workspace,
            context_id="e2e-auditor-v001",
            material=True,
        )
        assert first_audit.manifest.disposition.value == "revision_required"
        revision = _publish_revision(
            tmp_path,
            workspace,
            context_id="e2e-reviser-v002",
        )
        current_draft = revision.draft
        assert current_draft.manifest.draft_version == "v002"
        clean_audit = _publish_audit(
            tmp_path,
            workspace,
            context_id="e2e-auditor-v002",
            material=False,
        )
    else:
        current_draft = initial
        clean_audit = _publish_audit(
            tmp_path,
            workspace,
            context_id="e2e-auditor-v001",
            material=False,
        )

    assert clean_audit.human_review is not None
    assert clean_audit.manifest.p0_count == 0
    assert clean_audit.manifest.p1_count == 0
    target_sha256 = clean_audit.human_review.approval_target_sha256

    approval = approve_job(
        workspace.root,
        approval_target_sha256=target_sha256,
        reviewer="synthetic-human-reviewer",
        basis="explicit synthetic human review for the full E2E fixture",
        confirm=True,
        unresolved_notes=[],
    )
    assert approval.record.confirmed_by_user is True
    assert approval.record.approval_target_sha256 == target_sha256
    assert approval.record.approved_draft_version == current_draft.manifest.draft_version

    exported = export_job(workspace.root)
    current_path = workspace.safe_path(current_draft.manifest.draft_artifact.relative_storage_path)
    final_path = workspace.safe_path(exported.manifest.final_artifact.relative_storage_path)
    assert final_path.read_bytes() == current_path.read_bytes()
    assert exported.manifest.final_artifact.content_sha256 == target_sha256
    assert exported.package_manifest.byte_exact is True
    if with_revision:
        assert final_path.read_bytes() != initial_bytes

    transition_bytes = workspace.transitions_jsonl.read_bytes()
    approval_bytes = workspace.approval_records_jsonl.read_bytes()
    replayed_approval = approve_job(
        workspace.root,
        approval_target_sha256=target_sha256,
        reviewer="synthetic-human-reviewer",
        basis="explicit synthetic human review for the full E2E fixture",
        confirm=True,
        unresolved_notes=[],
    )
    replayed_export = export_job(workspace.root)
    assert replayed_approval.reused is True
    assert replayed_export.reused is True
    assert workspace.transitions_jsonl.read_bytes() == transition_bytes
    assert workspace.approval_records_jsonl.read_bytes() == approval_bytes

    expected_states = [
        JobState.CREATED,
        JobState.AUTHORIZED,
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.EVIDENCE_READY,
        JobState.DRAFTED,
        JobState.AUDITED,
    ]
    if with_revision:
        expected_states.extend(
            [
                JobState.REVISION_REQUIRED,
                JobState.DRAFTED,
                JobState.AUDITED,
            ]
        )
    expected_states.extend(
        [
            JobState.HUMAN_REVIEW_READY,
            JobState.HUMAN_APPROVED,
            JobState.EXPORTED,
        ]
    )
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [record.to_state for record in transitions] == expected_states

    validated = validate_job(workspace.root)
    status, status_exit = job_status(workspace.root)
    assert validated["state"] == JobState.EXPORTED.value
    assert status_exit == 0
    for section in (
        "artifact_integrity",
        "normalization_integrity",
        "transcription_integrity",
        "evidence_integrity",
        "authoring_integrity",
        "approval_integrity",
        "export_integrity",
    ):
        assert status[section]["status"] == "valid"
