from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

import pytest
from test_transcription_evidence import (
    _application_transcribed_job,
)
from test_transcription_evidence import (
    _response as _evidence_response,
)
from test_transcription_evidence import (
    _write_response as _write_evidence_response,
)
from typer.testing import CliRunner

import video_evidence_pipeline.application as application_module
import video_evidence_pipeline.authoring.pipeline as authoring_pipeline_module
from video_evidence_pipeline.application import (
    _import_audit_fixture,
    _import_authoring_fixture,
    _import_evidence_fixture,
    _import_revision_fixture,
    import_audit_job,
    import_authoring_job,
    import_revision_job,
    job_status,
    prepare_audit_job,
    prepare_authoring_job,
    prepare_evidence_job,
    prepare_revision_job,
    recover_job,
    validate_job,
)
from video_evidence_pipeline.authoring.pipeline import (
    _response_is_fixture,
    cleanup_attempt,
    create_attempt_directory,
    load_authoring_request,
    load_length_block,
    prepare_authoring_request,
    stage_audit,
)
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.domain.authoring import (
    AgentExecutionMode,
    AgentIdentity,
    AgentRole,
    AuditClaimRecord,
    AuditFinding,
    AuditResponseManifest,
    AuditSeverity,
    AuthoringResponseManifest,
    ClaimComparisonStatus,
    ClaimConfidence,
    ClaimRecord,
    ClaimType,
    DraftSpan,
    FixedEvidenceBundle,
    HumanReviewBundle,
    HumanReviewManifest,
    RequirementCoverage,
    RequirementMap,
    RequirementMapEntry,
    ResolutionDecision,
    ResolutionRecord,
    RevisionResponseManifest,
)
from video_evidence_pipeline.domain.evidence import AnalyzerExecutionMode
from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    SourceKind,
    SourceSpec,
    load_job_yaml,
)
from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.jsonl import read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace


def _write_model(path: Path, value: object) -> None:
    path.write_bytes(pretty_json_bytes(value))


def _identity(
    role: AgentRole,
    context_id: str,
    *,
    execution_mode: AgentExecutionMode = AgentExecutionMode.TEST_FIXTURE,
) -> AgentIdentity:
    return AgentIdentity(
        role=role,
        provider="local-offline-provider",
        model=f"{role.value}-model",
        version="1.0",
        executor="separate-agent-exchange",
        context_id=context_id,
        execution_mode=execution_mode,
        external_api_used=False,
    )


def _evidence_ready_job(
    tmp_path: Path,
    job_id: str,
    *,
    min_chars: int | None = None,
    max_chars: int | None = None,
    maximum_length_adjustments: int = 3,
):
    workspace, _transcription = _application_transcribed_job(
        tmp_path,
        job_id,
        min_chars=min_chars,
        max_chars=max_chars,
        maximum_length_adjustments=maximum_length_adjustments,
    )
    repository_root = workspace.jobs_root.parent.parent
    shutil.copytree(
        Path(__file__).parents[1] / ".agents",
        repository_root / ".agents",
        dirs_exist_ok=True,
    )
    request = prepare_evidence_job(workspace.root).request
    response_path = tmp_path / f"{job_id}-evidence-response.json"
    _write_evidence_response(
        response_path,
        _evidence_response(request, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE),
    )
    imported = _import_evidence_fixture(workspace.root, response_path)
    assert imported.evidence_records
    return workspace


def _set_skill_lifecycle(workspace: JobWorkspace, skill_id: str, lifecycle: str) -> None:
    repository_root = workspace.jobs_root.parent.parent
    registry_path = repository_root / ".agents" / "skill-system.toml"
    payload = registry_path.read_text(encoding="utf-8")
    entry_start = payload.index(f'id = "{skill_id}"')
    lifecycle_start = payload.index("lifecycle = ", entry_start)
    lifecycle_end = payload.index("\n", lifecycle_start)
    registry_path.write_text(
        payload[:lifecycle_start] + f'lifecycle = "{lifecycle}"' + payload[lifecycle_end:],
        encoding="utf-8",
    )


def _mutate_skill_content(workspace: JobWorkspace, skill_id: str) -> None:
    repository_root = workspace.jobs_root.parent.parent
    skill_path = repository_root / ".agents" / "skills" / skill_id / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\n<!-- test snapshot mutation -->\n",
        encoding="utf-8",
    )


def _mutate_skill_evaluation(workspace: JobWorkspace, skill_id: str) -> None:
    repository_root = workspace.jobs_root.parent.parent
    evaluation_path = repository_root / ".agents" / "skill-evals" / f"{skill_id}.toml"
    evaluation_path.write_text(
        evaluation_path.read_text(encoding="utf-8") + "\n# test snapshot mutation\n",
        encoding="utf-8",
    )


def _requirement_map(request, claim_id: str, evidence_id: str) -> RequirementMap:
    return RequirementMap(
        entries=[
            RequirementMapEntry(
                requirement_id=item.requirement_id,
                requirement_text=item.text,
                coverage=RequirementCoverage.SATISFIED,
                claim_ids=[claim_id],
                evidence_ids=[evidence_id],
                explanation="The selected evidence supports the stated requirement.",
            )
            for item in request.output_constraints.requirements
        ]
    )


def _author_response(
    request,
    *,
    context_id: str = "author-context-1",
    execution_mode: AgentExecutionMode = AgentExecutionMode.TEST_FIXTURE,
) -> AuthoringResponseManifest:
    draft = "😀 application evidence is the key event."
    text = "application evidence"
    start = draft.index(text)
    span = DraftSpan(start=start, end=start + len(text), text=text)
    claim = ClaimRecord(
        claim_id="C000001",
        draft_span=span,
        text=text,
        claim_type=ClaimType.VIDEO_FACT,
        evidence_ids=["E000001"],
        requirement_ids=[item.requirement_id for item in request.output_constraints.requirements],
        author_confidence=ClaimConfidence.HIGH,
    )
    return AuthoringResponseManifest(
        request_fingerprint=request.request_fingerprint,
        author=_identity(AgentRole.AUTHOR, context_id, execution_mode=execution_mode),
        draft_markdown=draft,
        claims=[claim],
        requirement_map=_requirement_map(request, claim.claim_id, "E000001"),
        created_at=datetime.now(UTC),
    )


def _audit_response(
    request,
    *,
    context_id: str = "audit-context-1",
    material: bool = False,
    execution_mode: AgentExecutionMode = AgentExecutionMode.TEST_FIXTURE,
) -> AuditResponseManifest:
    text = "application evidence"
    if text not in request.target_draft_markdown:
        start = request.target_draft_markdown.index("[REVISION-")
        end = request.target_draft_markdown.index("]", start) + 1
        text = request.target_draft_markdown[start:end]
    start = request.target_draft_markdown.index(text)
    claim = AuditClaimRecord(
        audit_claim_id="AC000001",
        draft_span=DraftSpan(start=start, end=start + len(text), text=text),
        text=text,
        claim_type=ClaimType.VIDEO_FACT,
        evidence_ids=["E000001"],
    )
    findings = (
        [
            AuditFinding(
                finding_id="AF000001",
                severity=AuditSeverity.P1,
                category="evidence-binding",
                rationale="The material claim requires a bounded revision.",
                draft_span=claim.draft_span,
                audit_claim_ids=[claim.audit_claim_id],
                requirement_ids=["R000001"],
                evidence_ids=["E000001"],
                source_refs=list(request.fixed_evidence.evidence_records[0].source_refs),
                confidence=ClaimConfidence.HIGH,
            )
        ]
        if material
        else []
    )
    return AuditResponseManifest(
        request_fingerprint=request.request_fingerprint,
        target_draft_sha256=request.target_draft_sha256,
        auditor=_identity(AgentRole.AUDITOR, context_id, execution_mode=execution_mode),
        audit_claims=[claim],
        findings=findings,
        created_at=datetime.now(UTC),
    )


def _revision_response(
    request,
    *,
    context_id: str,
    replacement_suffix: str = "",
    execution_mode: AgentExecutionMode = AgentExecutionMode.TEST_FIXTURE,
) -> RevisionResponseManifest:
    anchor = request.audit_findings[0].draft_span
    assert anchor is not None
    text = f"[REVISION-{request.next_draft_version}]{replacement_suffix}"
    draft = (
        request.target_draft_markdown[: anchor.start]
        + text
        + request.target_draft_markdown[anchor.end :]
    )
    claim_start = anchor.start
    claim = ClaimRecord(
        claim_id="C000001",
        draft_span=DraftSpan(
            start=claim_start,
            end=claim_start + len(text),
            text=text,
        ),
        text=text,
        claim_type=ClaimType.VIDEO_FACT,
        evidence_ids=["E000001"],
        requirement_ids=[item.requirement_id for item in request.output_constraints.requirements],
        author_confidence=ClaimConfidence.HIGH,
    )
    changed_spans: list[DraftSpan] = []
    removed_spans: list[DraftSpan] = []
    matcher = SequenceMatcher(None, request.target_draft_markdown, draft, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if new_start < new_end:
            changed_spans.append(
                DraftSpan(
                    start=new_start,
                    end=new_end,
                    text=draft[new_start:new_end],
                )
            )
        if old_start < old_end:
            removed_spans.append(
                DraftSpan(
                    start=old_start,
                    end=old_end,
                    text=request.target_draft_markdown[old_start:old_end],
                )
            )
    resolutions = [
        ResolutionRecord(
            finding_id=item.finding_id,
            decision=ResolutionDecision.ACCEPT,
            resolution="The bounded draft change addresses the audit finding.",
            changed_spans=changed_spans,
            removed_spans=removed_spans,
            evidence_ids=["E000001"],
        )
        for item in request.audit_findings
    ]
    return RevisionResponseManifest(
        request_fingerprint=request.request_fingerprint,
        reviser=_identity(AgentRole.REVISER, context_id, execution_mode=execution_mode),
        draft_markdown=draft,
        claims=[claim],
        requirement_map=_requirement_map(request, claim.claim_id, "E000001"),
        resolutions=resolutions,
        created_at=datetime.now(UTC),
    )


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _recursive_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _recursive_keys(item)}
    return set()


def test_authoring_requires_selected_evidence_before_skill_loading(tmp_path: Path) -> None:
    job = JobSpec(
        job_id="issue15-no-evidence",
        created_at=datetime.now(UTC),
        source=SourceSpec(kind=SourceKind.LOCAL_FILE, locator="fixture.mkv"),
        assignment=AssignmentSpec(task_type="report", language="en", requirements=[]),
        output=OutputSpec(format="markdown"),
    )
    workspace = JobWorkspace.create(tmp_path, job.job_id)
    fixed = FixedEvidenceBundle.model_construct(
        job_id=job.job_id,
        fixed_evidence_fingerprint="1" * 64,
        evidence_request=None,
        evidence_manifest=None,
        evidence_records=[],
        ambiguity_records=[],
    )
    with pytest.raises(VepError) as no_evidence_error:
        prepare_authoring_request(
            workspace,
            job,
            fixed,
            "draft-video-report",
        )
    assert no_evidence_error.value.code == "AUTHORING_EVIDENCE_REQUIRED"
    with pytest.raises(VepError) as no_evidence_load_error:
        load_authoring_request(workspace, job, fixed)
    assert no_evidence_load_error.value.code == "AUTHORING_EVIDENCE_REQUIRED"


def test_straight_author_audit_human_review_and_cli_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _evidence_ready_job(tmp_path, "issue15-straight")
    _set_skill_lifecycle(workspace, "draft-video-report", "draft")
    author_request = prepare_authoring_job(workspace.root, "draft-video-report").request
    assert author_request.skill.lifecycle == "draft"
    assert author_request.skill.automatic_trigger is False

    fixture_path = tmp_path / "fixture-author.json"
    _write_model(
        fixture_path,
        _author_response(
            author_request,
            execution_mode=AgentExecutionMode.TEST_FIXTURE,
        ),
    )
    try:
        import_authoring_job(workspace.root, fixture_path)
    except VepError as exc:
        assert exc.code == "FIXTURE_AGENT_FORBIDDEN"
    else:  # pragma: no cover - fail-closed assertion
        raise AssertionError("production authoring import accepted a fixture response")

    latest_identity = _identity(
        AgentRole.AUTHOR,
        "latest-context",
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    ).model_copy(update={"model": "model-latest"})
    assert _response_is_fixture(latest_identity) is False
    assert _response_is_fixture(latest_identity.model_copy(update={"model": "test-model"})) is True

    external_path = tmp_path / "external-author.json"
    _write_model(
        external_path,
        _author_response(
            author_request,
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    with pytest.raises(VepError) as draft_skill_error:
        import_authoring_job(workspace.root, external_path)
    assert draft_skill_error.value.code == "DRAFT_SKILL_FORBIDDEN"

    oversize_path = tmp_path / "oversize-author.json"
    oversize_draft = ("😀" * (1024 * 1024 + 1)) + " application evidence"
    text = "application evidence"
    start = oversize_draft.index(text)
    base_response = _author_response(author_request)
    oversize_claim = base_response.claims[0].model_copy(
        update={
            "draft_span": DraftSpan(start=start, end=start + len(text), text=text),
        }
    )
    oversize_response = AuthoringResponseManifest.model_validate(
        {
            **base_response.model_dump(mode="json"),
            "draft_markdown": oversize_draft,
            "claims": [oversize_claim.model_dump(mode="json")],
        }
    )
    _write_model(oversize_path, oversize_response)
    with pytest.raises(VepError) as oversize_error:
        _import_authoring_fixture(workspace.root, oversize_path)
    assert oversize_error.value.code == "AUTHORING_OUTPUT_INVALID"

    fake_evidence_path = tmp_path / "fake-evidence-author.json"
    fake_claim = base_response.claims[0].model_copy(update={"evidence_ids": ["E999999"]})
    fake_response = base_response.model_copy(update={"claims": [fake_claim]})
    _write_model(fake_evidence_path, fake_response)
    with pytest.raises(VepError) as fake_evidence_error:
        _import_authoring_fixture(workspace.root, fake_evidence_path)
    assert fake_evidence_error.value.code == "AUTHORING_RESPONSE_INVALID"

    reciprocal_path = tmp_path / "non-reciprocal-author.json"
    reciprocal_claim = base_response.claims[0].model_copy(
        update={"requirement_ids": base_response.claims[0].requirement_ids[1:]}
    )
    _write_model(
        reciprocal_path,
        base_response.model_copy(update={"claims": [reciprocal_claim]}),
    )
    with pytest.raises(VepError) as reciprocal_error:
        _import_authoring_fixture(workspace.root, reciprocal_path)
    assert reciprocal_error.value.code == "AUTHORING_RESPONSE_INVALID"

    omitted_path = tmp_path / "omitted-requirement-author.json"
    omitted_response = base_response.model_copy(
        update={
            "requirement_map": RequirementMap(entries=[base_response.requirement_map.entries[0]])
        }
    )
    _write_model(omitted_path, omitted_response)
    with pytest.raises(VepError) as omitted_error:
        _import_authoring_fixture(workspace.root, omitted_path)
    assert omitted_error.value.code == "AUTHORING_RESPONSE_INVALID"

    author_path = tmp_path / "author-response.json"
    _write_model(author_path, _author_response(author_request))
    evidence_artifact = author_request.fixed_evidence.evidence_manifest.evidence_artifact
    evidence_path = workspace.safe_path(evidence_artifact.relative_storage_path)
    evidence_bytes = evidence_path.read_bytes()
    evidence_metadata = evidence_path.stat()
    original_stage_initial = application_module.stage_initial_draft

    def tamper_after_stage(*args, **kwargs):
        staged = original_stage_initial(*args, **kwargs)
        payload = bytearray(evidence_path.read_bytes())
        payload[0] ^= 0x01
        evidence_path.write_bytes(payload)
        return staged

    monkeypatch.setattr(application_module, "stage_initial_draft", tamper_after_stage)
    with pytest.raises(VepError):
        _import_authoring_fixture(workspace.root, author_path)
    assert not workspace.safe_path("manifests/authoring/draft-v001.json").exists()
    evidence_path.write_bytes(evidence_bytes)
    os.utime(
        evidence_path,
        ns=(evidence_metadata.st_atime_ns, evidence_metadata.st_mtime_ns),
    )
    monkeypatch.setattr(application_module, "stage_initial_draft", original_stage_initial)
    draft = _import_authoring_fixture(workspace.root, author_path)
    assert draft.manifest.draft_version == "v001"
    assert draft.manifest.revision_count == 0

    audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    request_path = workspace.safe_path("authoring/requests/audit-v001.json")
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    forbidden = {
        "author",
        "author_claims",
        "claims",
        "agent_run",
        "authoring_context_ids",
        "context_id",
        "reasoning",
        "prompt_history",
    }
    assert forbidden.isdisjoint(_recursive_keys(request_payload))

    audit_path = tmp_path / "audit-response.json"
    audit_response = _audit_response(audit_request)
    _write_model(audit_path, audit_response)
    source_less_path = tmp_path / "source-less-material-audit.json"
    source_less = _audit_response(audit_request, material=True)
    _write_model(
        source_less_path,
        source_less.model_copy(
            update={"findings": [source_less.findings[0].model_copy(update={"source_refs": []})]}
        ),
    )
    with pytest.raises(VepError) as source_error:
        _import_audit_fixture(workspace.root, source_less_path)
    assert source_error.value.code == "AUDIT_RESPONSE_INVALID"
    same_context_path = tmp_path / "same-context-audit.json"
    _write_model(
        same_context_path,
        _audit_response(audit_request, context_id="author-context-1"),
    )
    with pytest.raises(VepError) as same_context_error:
        _import_audit_fixture(workspace.root, same_context_path)
    assert same_context_error.value.code == "AUDIT_CONTEXT_NOT_INDEPENDENT"

    wrong_target_path = tmp_path / "wrong-target-audit.json"
    _write_model(
        wrong_target_path,
        audit_response.model_copy(update={"target_draft_sha256": "2" * 64}),
    )
    with pytest.raises(VepError) as wrong_target_error:
        _import_audit_fixture(workspace.root, wrong_target_path)
    assert wrong_target_error.value.code == "AUDIT_RESPONSE_INVALID"
    attempt = create_attempt_directory(workspace)
    try:
        with pytest.raises(VepError) as context_error:
            stage_audit(
                workspace,
                load_job_yaml(workspace.job_yaml),
                audit_request,
                audit_response,
                draft,
                (),
                attempt,
            )
        assert context_error.value.code == "AUDIT_REQUEST_STALE"
    finally:
        cleanup_attempt(workspace, attempt)
    audit = _import_audit_fixture(workspace.root, audit_path)
    assert audit.manifest.disposition.value == "human_review_ready"
    assert audit.human_review is not None
    assert audit.claim_comparisons[0].status is ClaimComparisonStatus.MATCHED
    comparison_path = workspace.safe_path(
        audit.manifest.claim_comparison_artifact.relative_storage_path
    )
    assert comparison_path.is_file()

    human_bundle = json.loads(
        workspace.safe_path(audit.human_review.bundle_artifact.relative_storage_path).read_text(
            encoding="utf-8"
        )
    )
    assert human_bundle["is_approval"] is False
    assert validate_job(workspace.root)["state"] == JobState.HUMAN_REVIEW_READY.value
    status, exit_code = job_status(workspace.root)
    assert exit_code == 0
    assert status["authoring_integrity"]["status"] == "valid"

    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in transitions[-3:]] == [
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.HUMAN_REVIEW_READY,
    ]
    assert _import_authoring_fixture(workspace.root, author_path).reused is True
    assert _import_audit_fixture(workspace.root, audit_path).reused is True
    assert read_jsonl(workspace.transitions_jsonl, TransitionRecord) == transitions

    runner = CliRunner()
    prepared = runner.invoke(
        app,
        [
            "audit",
            "prepare",
            str(workspace.root),
            "--skill",
            "independent-content-audit",
            "--json",
        ],
    )
    assert prepared.exit_code == 0
    assert json.loads(prepared.stdout)["reused"] is True

    human_stage_path = workspace.safe_path("manifests/stages/human-review-v001.json")
    human_stage = json.loads(human_stage_path.read_text(encoding="utf-8"))
    _write_model(human_stage_path, {**human_stage, "status": "failed"})
    with pytest.raises(VepError) as stage_tamper_error:
        validate_job(workspace.root)
    assert stage_tamper_error.value.code == "HUMAN_REVIEW_BUNDLE_INVALID"


@pytest.mark.parametrize("author_skill", ["draft-video-report", "draft-video-summary"])
def test_public_production_straight_routes_use_committed_active_skills(
    tmp_path: Path,
    author_skill: str,
) -> None:
    suffix = author_skill.removeprefix("draft-video-")
    workspace = _evidence_ready_job(tmp_path, f"issue15-production-{suffix}")
    author_request = prepare_authoring_job(workspace.root, author_skill).request
    assert author_request.skill.lifecycle == "active"
    assert author_request.skill.automatic_trigger is False
    assert author_request.skill.side_effect_policy == "instruction-only"

    denied_response = _author_response(
        author_request,
        context_id=f"production-{suffix}-author-denied",
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    denied_response = denied_response.model_copy(
        update={"author": denied_response.author.model_copy(update={"external_api_used": True})}
    )
    denied_path = tmp_path / f"{suffix}-external-api-denied.json"
    _write_model(denied_path, denied_response)
    with pytest.raises(VepError) as permission_error:
        import_authoring_job(workspace.root, denied_path)
    assert permission_error.value.code == "EXTERNAL_API_NOT_AUTHORIZED"

    author_path = tmp_path / f"{suffix}-production-author.json"
    author_response = _author_response(
        author_request,
        context_id=f"production-{suffix}-author",
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    _write_model(author_path, author_response)
    draft = import_authoring_job(workspace.root, author_path)
    assert draft.agent_run.identity.execution_mode is AgentExecutionMode.EXTERNAL_EXCHANGE
    status, exit_code = job_status(workspace.root)
    assert status["stored_state"] == JobState.DRAFTED.value
    assert exit_code == 0

    audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    assert audit_request.skill.lifecycle == "active"
    same_context_path = tmp_path / f"{suffix}-production-same-context-audit.json"
    _write_model(
        same_context_path,
        _audit_response(
            audit_request,
            context_id=author_response.author.context_id,
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    with pytest.raises(VepError) as context_error:
        import_audit_job(workspace.root, same_context_path)
    assert context_error.value.code == "AUDIT_CONTEXT_NOT_INDEPENDENT"

    audit_path = tmp_path / f"{suffix}-production-audit.json"
    audit_response = _audit_response(
        audit_request,
        context_id=f"production-{suffix}-auditor",
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    assert audit_response.auditor.context_id != author_response.author.context_id
    _write_model(audit_path, audit_response)
    audit = import_audit_job(workspace.root, audit_path)
    assert audit.agent_run.identity.execution_mode is AgentExecutionMode.EXTERNAL_EXCHANGE
    status, exit_code = job_status(workspace.root)
    assert status["stored_state"] == JobState.HUMAN_REVIEW_READY.value
    assert exit_code == 0
    assert validate_job(workspace.root)["state"] == JobState.HUMAN_REVIEW_READY.value


def test_public_production_revision_route_uses_distinct_external_contexts(
    tmp_path: Path,
) -> None:
    workspace = _evidence_ready_job(tmp_path, "issue15-production-revision")
    author_request = prepare_authoring_job(workspace.root, "draft-video-report").request
    author_path = tmp_path / "production-revision-author.json"
    author_response = _author_response(
        author_request,
        context_id="production-revision-author",
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    _write_model(author_path, author_response)
    import_authoring_job(workspace.root, author_path)

    first_audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    first_audit_path = tmp_path / "production-revision-audit-1.json"
    first_audit_response = _audit_response(
        first_audit_request,
        context_id="production-revision-auditor-1",
        material=True,
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    denied_audit_path = tmp_path / "production-revision-audit-api-denied.json"
    _write_model(
        denied_audit_path,
        first_audit_response.model_copy(
            update={
                "auditor": first_audit_response.auditor.model_copy(
                    update={"external_api_used": True}
                )
            }
        ),
    )
    with pytest.raises(VepError) as audit_permission_error:
        import_audit_job(workspace.root, denied_audit_path)
    assert audit_permission_error.value.code == "EXTERNAL_API_NOT_AUTHORIZED"

    _write_model(first_audit_path, first_audit_response)
    import_audit_job(workspace.root, first_audit_path)
    status, exit_code = job_status(workspace.root)
    assert status["stored_state"] == JobState.REVISION_REQUIRED.value
    assert exit_code == 0

    revision_request = prepare_revision_job(workspace.root, "revise-from-audit").request
    assert revision_request.skill.lifecycle == "active"
    revision_path = tmp_path / "production-revision-response.json"
    revision_response = _revision_response(
        revision_request,
        context_id="production-reviser-1",
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    denied_revision_path = tmp_path / "production-revision-api-denied.json"
    _write_model(
        denied_revision_path,
        revision_response.model_copy(
            update={
                "reviser": revision_response.reviser.model_copy(update={"external_api_used": True})
            }
        ),
    )
    with pytest.raises(VepError) as revision_permission_error:
        import_revision_job(workspace.root, denied_revision_path)
    assert revision_permission_error.value.code == "EXTERNAL_API_NOT_AUTHORIZED"

    _write_model(revision_path, revision_response)
    revised = import_revision_job(workspace.root, revision_path)
    assert revised.draft.agent_run.identity.execution_mode is AgentExecutionMode.EXTERNAL_EXCHANGE
    status, exit_code = job_status(workspace.root)
    assert status["stored_state"] == JobState.DRAFTED.value
    assert exit_code == 0

    second_audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    second_audit_path = tmp_path / "production-revision-audit-2.json"
    second_audit_response = _audit_response(
        second_audit_request,
        context_id="production-revision-auditor-2",
        material=False,
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    _write_model(second_audit_path, second_audit_response)
    import_audit_job(workspace.root, second_audit_path)
    contexts = {
        author_response.author.context_id,
        first_audit_response.auditor.context_id,
        revision_response.reviser.context_id,
        second_audit_response.auditor.context_id,
    }
    assert len(contexts) == 4
    status, exit_code = job_status(workspace.root)
    assert status["stored_state"] == JobState.HUMAN_REVIEW_READY.value
    assert exit_code == 0
    assert validate_job(workspace.root)["state"] == JobState.HUMAN_REVIEW_READY.value


def test_skill_snapshot_history_is_immutable_but_pending_production_import_is_current(
    tmp_path: Path,
) -> None:
    completed_root = tmp_path / "completed"
    completed_root.mkdir()
    completed = _evidence_ready_job(completed_root, "issue15-skill-history")
    _set_skill_lifecycle(completed, "draft-video-report", "draft")
    request = prepare_authoring_job(completed.root, "draft-video-report").request
    response_path = completed_root / "skill-history-author.json"
    _write_model(
        response_path,
        _author_response(
            request,
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    _set_skill_lifecycle(completed, "draft-video-report", "active")
    imported = import_authoring_job(completed.root, response_path)
    assert imported.manifest.draft_version == "v001"
    _publish_audit(
        completed_root,
        completed,
        context_id="skill-history-audit",
        material=False,
    )
    assert validate_job(completed.root)["state"] == JobState.HUMAN_REVIEW_READY.value
    _mutate_skill_content(completed, "draft-video-report")
    assert prepare_authoring_job(completed.root, "draft-video-report").reused is True
    assert validate_job(completed.root)["state"] == JobState.HUMAN_REVIEW_READY.value

    pending_root = tmp_path / "pending"
    pending_root.mkdir()
    pending = _evidence_ready_job(pending_root, "issue15-skill-drift")
    _set_skill_lifecycle(pending, "draft-video-report", "draft")
    pending_request = prepare_authoring_job(pending.root, "draft-video-report").request
    pending_path = pending_root / "skill-drift-author.json"
    _write_model(
        pending_path,
        _author_response(
            pending_request,
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    _set_skill_lifecycle(pending, "draft-video-report", "active")
    _mutate_skill_content(pending, "draft-video-report")
    with pytest.raises(VepError) as drift_error:
        import_authoring_job(pending.root, pending_path)
    assert drift_error.value.code == "SKILL_SNAPSHOT_STALE"
    assert (
        load_authoring_request(
            pending,
            load_job_yaml(pending.job_yaml),
            pending_request.fixed_evidence,
        )
        == pending_request
    )


def test_author_import_replay_reports_current_state_and_latest_draft_version(
    tmp_path: Path,
) -> None:
    workspace = _evidence_ready_job(tmp_path, "issue15-author-replay")
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    _set_skill_lifecycle(workspace, "draft-video-report", "active")
    response_path = tmp_path / "author-replay.json"
    _write_model(
        response_path,
        _author_response(
            request,
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["author", "import", str(workspace.root), "--response", str(response_path), "--json"],
    )
    assert first.exit_code == 0
    _publish_audit(
        tmp_path,
        workspace,
        context_id="author-replay-audit-1",
        material=True,
    )
    _publish_revision(tmp_path, workspace, context_id="author-replay-revision")
    _publish_audit(
        tmp_path,
        workspace,
        context_id="author-replay-audit-2",
        material=False,
    )
    replay = runner.invoke(
        app,
        ["author", "import", str(workspace.root), "--response", str(response_path), "--json"],
    )
    assert replay.exit_code == 0
    payload = json.loads(replay.stdout)
    assert payload["state"] == JobState.HUMAN_REVIEW_READY.value
    assert payload["draft_version"] == "v002"
    assert payload["imported_draft_version"] == "v001"
    assert payload["draft_sha256"] != payload["imported_draft_sha256"]
    assert payload["reused"] is True


def _publish_initial_draft(
    tmp_path: Path,
    job_id: str,
    *,
    min_chars: int | None = None,
    max_chars: int | None = None,
    maximum_length_adjustments: int = 3,
):
    workspace = _evidence_ready_job(
        tmp_path,
        job_id,
        min_chars=min_chars,
        max_chars=max_chars,
        maximum_length_adjustments=maximum_length_adjustments,
    )
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    response_path = tmp_path / f"{job_id}-author.json"
    _write_model(response_path, _author_response(request))
    draft = _import_authoring_fixture(workspace.root, response_path)
    return workspace, draft


def _publish_audit(
    tmp_path: Path,
    workspace,
    *,
    context_id: str,
    material: bool,
):
    request = prepare_audit_job(workspace.root, "independent-content-audit").request
    response_path = tmp_path / f"{request.target_draft_version}-{context_id}.json"
    _write_model(
        response_path,
        _audit_response(request, context_id=context_id, material=material),
    )
    return _import_audit_fixture(workspace.root, response_path)


def _publish_revision(
    tmp_path: Path,
    workspace,
    *,
    context_id: str,
):
    request = prepare_revision_job(workspace.root, "revise-from-audit").request
    response_path = tmp_path / f"{request.next_draft_version}-{context_id}.json"
    _write_model(response_path, _revision_response(request, context_id=context_id))
    return _import_revision_fixture(workspace.root, response_path)


def _human_review_ready_job(tmp_path: Path, job_id: str) -> JobWorkspace:
    workspace, _draft = _publish_initial_draft(tmp_path, job_id)
    clean = _publish_audit(
        tmp_path,
        workspace,
        context_id=f"{job_id}-clean-audit",
        material=False,
    )
    assert clean.human_review is not None
    return workspace


def _replace_human_review_artifact(workspace: JobWorkspace, payload: bytes) -> None:
    manifest_path = workspace.human_review_manifest_json
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_path = workspace.safe_path(manifest_payload["bundle_artifact"]["relative_storage_path"])
    bundle_version = json.loads(bundle_path.read_text(encoding="utf-8"))["current_draft_version"]
    digest = sha256_bytes(payload)
    artifact = {
        **manifest_payload["bundle_artifact"],
        "artifact_id": f"human_review_bundle-{digest[:16]}",
        "content_sha256": digest,
        "size_bytes": len(payload),
    }
    manifest_payload["bundle_artifact"] = artifact
    provisional = HumanReviewManifest.model_validate(manifest_payload)
    manifest_payload["bundle_fingerprint"] = (
        authoring_pipeline_module._human_review_manifest_fingerprint(provisional)
    )
    stage_path = workspace.safe_path(f"manifests/stages/human-review-{bundle_version}.json")
    stage_payload = json.loads(stage_path.read_text(encoding="utf-8"))
    stage_payload["configuration_sha256"] = manifest_payload["bundle_fingerprint"]
    stage_payload["output_artifacts"] = [artifact]
    bundle_path.write_bytes(payload)
    _write_model(manifest_path, manifest_payload)
    _write_model(stage_path, stage_payload)


def _project_draft_manifest_to_pre_pr46(workspace: JobWorkspace) -> None:
    draft_path = workspace.safe_path("manifests/authoring/draft-v001.json")
    draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
    for field in (
        "length_check",
        "semantic_revision_count",
        "length_adjustment_count",
    ):
        draft_payload.pop(field)
    draft_model = authoring_pipeline_module.DraftManifest.model_validate(draft_payload)
    draft_payload["bundle_fingerprint"] = authoring_pipeline_module._draft_fingerprint(draft_model)
    draft_stage_path = workspace.safe_path("manifests/stages/authoring-v001.json")
    draft_stage = json.loads(draft_stage_path.read_text(encoding="utf-8"))
    draft_stage["configuration_sha256"] = draft_payload["bundle_fingerprint"]
    human_manifest = json.loads(workspace.human_review_manifest_json.read_text(encoding="utf-8"))
    human_manifest["draft_bundle_fingerprint"] = draft_payload["bundle_fingerprint"]
    _write_model(draft_path, draft_payload)
    _write_model(draft_stage_path, draft_stage)
    _write_model(workspace.human_review_manifest_json, human_manifest)


def test_pre_pr46_human_review_historical_projection_deep_validates(tmp_path: Path) -> None:
    workspace = _human_review_ready_job(tmp_path, "issue47-human-review-legacy")
    _project_draft_manifest_to_pre_pr46(workspace)
    manifest = json.loads(workspace.human_review_manifest_json.read_text(encoding="utf-8"))
    bundle_path = workspace.safe_path(manifest["bundle_artifact"]["relative_storage_path"])
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    historical_fields = {
        "length_check",
        "semantic_revision_count",
        "length_adjustment_count",
    }
    for field in historical_fields:
        payload.pop(field)
    legacy_bytes = pretty_json_bytes(payload)
    _replace_human_review_artifact(workspace, legacy_bytes)

    stored = HumanReviewBundle.model_validate_json(legacy_bytes, strict=True)
    assert not historical_fields & stored.model_fields_set
    assert authoring_pipeline_module.canonical_human_review_bundle_bytes(stored) == legacy_bytes
    assert validate_job(workspace.root, deep=True)["state"] == JobState.HUMAN_REVIEW_READY


def test_current_human_review_canonical_bytes_deep_validate(tmp_path: Path) -> None:
    workspace = _human_review_ready_job(tmp_path, "issue47-human-review-current")
    manifest = json.loads(workspace.human_review_manifest_json.read_text(encoding="utf-8"))
    bundle_path = workspace.safe_path(manifest["bundle_artifact"]["relative_storage_path"])
    current_bytes = bundle_path.read_bytes()
    stored = HumanReviewBundle.model_validate_json(current_bytes, strict=True)

    assert {
        "length_check",
        "semantic_revision_count",
        "length_adjustment_count",
    } <= stored.model_fields_set
    assert authoring_pipeline_module.canonical_human_review_bundle_bytes(stored) == current_bytes
    assert validate_job(workspace.root, deep=True)["state"] == JobState.HUMAN_REVIEW_READY


def test_human_review_legacy_projection_does_not_alias_current_null_bytes(
    tmp_path: Path,
) -> None:
    workspace = _human_review_ready_job(tmp_path, "issue47-human-review-shape")
    manifest = json.loads(workspace.human_review_manifest_json.read_text(encoding="utf-8"))
    bundle_path = workspace.safe_path(manifest["bundle_artifact"]["relative_storage_path"])
    current_payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    current_payload.update(
        {
            "length_check": None,
            "semantic_revision_count": None,
            "length_adjustment_count": None,
        }
    )
    current_bytes = pretty_json_bytes(current_payload)
    legacy_payload = dict(current_payload)
    for field in (
        "length_check",
        "semantic_revision_count",
        "length_adjustment_count",
    ):
        legacy_payload.pop(field)
    legacy_bytes = pretty_json_bytes(legacy_payload)
    current = HumanReviewBundle.model_validate_json(current_bytes, strict=True)
    legacy = HumanReviewBundle.model_validate_json(legacy_bytes, strict=True)

    assert authoring_pipeline_module.canonical_human_review_bundle_bytes(current) == current_bytes
    assert authoring_pipeline_module.canonical_human_review_bundle_bytes(legacy) == legacy_bytes
    assert sha256_bytes(current_bytes) != sha256_bytes(legacy_bytes)


def test_human_review_partial_historical_shape_fails_closed(tmp_path: Path) -> None:
    workspace = _human_review_ready_job(tmp_path, "issue47-human-review-partial")
    manifest = json.loads(workspace.human_review_manifest_json.read_text(encoding="utf-8"))
    bundle_path = workspace.safe_path(manifest["bundle_artifact"]["relative_storage_path"])
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload.pop("semantic_revision_count")
    payload.pop("length_adjustment_count")
    _replace_human_review_artifact(workspace, pretty_json_bytes(payload))

    with pytest.raises(VepError) as error:
        validate_job(workspace.root, deep=True)
    assert error.value.code == "HUMAN_REVIEW_BUNDLE_INVALID"


def test_human_review_content_tamper_and_noncanonical_bytes_fail_deep(
    tmp_path: Path,
) -> None:
    tamper_root = tmp_path / "tamper"
    tamper_root.mkdir()
    tampered = _human_review_ready_job(tamper_root, "issue47-human-review-tamper")
    manifest = json.loads(tampered.human_review_manifest_json.read_text(encoding="utf-8"))
    bundle_path = tampered.safe_path(manifest["bundle_artifact"]["relative_storage_path"])
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["p2_count"] += 1
    bundle_path.write_bytes(pretty_json_bytes(payload))
    with pytest.raises(VepError) as tamper_error:
        validate_job(tampered.root, deep=True)
    assert tamper_error.value.code == "AUTHORING_BUNDLE_TAMPERED"

    noncanonical_root = tmp_path / "noncanonical"
    noncanonical_root.mkdir()
    noncanonical = _human_review_ready_job(
        noncanonical_root,
        "issue47-human-review-noncanonical",
    )
    manifest = json.loads(noncanonical.human_review_manifest_json.read_text(encoding="utf-8"))
    bundle_path = noncanonical.safe_path(manifest["bundle_artifact"]["relative_storage_path"])
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    compact_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _replace_human_review_artifact(noncanonical, compact_bytes)
    with pytest.raises(VepError) as noncanonical_error:
        validate_job(noncanonical.root, deep=True)
    assert noncanonical_error.value.code == "HUMAN_REVIEW_BUNDLE_INVALID"


def test_human_review_wrong_artifact_sha_fails_deep(tmp_path: Path) -> None:
    workspace = _human_review_ready_job(tmp_path, "issue47-human-review-wrong-sha")
    manifest = json.loads(workspace.human_review_manifest_json.read_text(encoding="utf-8"))
    manifest["bundle_artifact"]["content_sha256"] = "0" * 64
    _write_model(workspace.human_review_manifest_json, manifest)

    with pytest.raises(VepError) as error:
        validate_job(workspace.root, deep=True)
    assert error.value.code == "AUTHORING_BUNDLE_TAMPERED"


def test_one_revision_then_clean_audit_reaches_human_review(tmp_path: Path) -> None:
    workspace, initial = _publish_initial_draft(tmp_path, "issue15-revision-clean")
    initial_bytes = workspace.safe_path(
        initial.manifest.draft_artifact.relative_storage_path
    ).read_bytes()
    first_audit = _publish_audit(
        tmp_path,
        workspace,
        context_id="audit-context-1",
        material=True,
    )
    assert first_audit.manifest.disposition.value == "revision_required"

    revised = _publish_revision(tmp_path, workspace, context_id="revision-context-1")
    assert revised.draft.manifest.draft_version == "v002"
    assert revised.draft.manifest.revision_count == 1
    assert (
        workspace.safe_path(initial.manifest.draft_artifact.relative_storage_path).read_bytes()
        == initial_bytes
    )

    reused_context_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    reused_context_path = tmp_path / "reused-audit-context.json"
    _write_model(
        reused_context_path,
        _audit_response(reused_context_request, context_id="audit-context-1"),
    )
    with pytest.raises(VepError) as reused_context_error:
        _import_audit_fixture(workspace.root, reused_context_path)
    assert reused_context_error.value.code == "AUDIT_CONTEXT_NOT_INDEPENDENT"

    clean = _publish_audit(
        tmp_path,
        workspace,
        context_id="audit-context-2",
        material=False,
    )
    assert clean.manifest.disposition.value == "human_review_ready"
    assert clean.human_review is not None
    human_review_bundle = json.loads(
        workspace.safe_path(clean.human_review.bundle_artifact.relative_storage_path).read_text(
            encoding="utf-8"
        )
    )
    assert human_review_bundle["semantic_revision_count"] == 1
    assert human_review_bundle["length_adjustment_count"] == 0
    assert clean.manifest.target_draft_version == "v002"
    assert validate_job(workspace.root)["state"] == JobState.HUMAN_REVIEW_READY.value
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in transitions[-6:]] == [
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.HUMAN_REVIEW_READY,
    ]
    resolution_path = workspace.safe_path(
        revised.manifest.resolution_artifact.relative_storage_path
    )
    resolution_path.write_bytes(resolution_path.read_bytes() + b" ")
    with pytest.raises(VepError) as tamper_error:
        validate_job(workspace.root)
    assert tamper_error.value.code == "AUTHORING_BUNDLE_TAMPERED"


def test_semantic_revision_reapplies_length_gate_without_spending_revision_budget(
    tmp_path: Path,
) -> None:
    workspace, initial = _publish_initial_draft(
        tmp_path,
        "issue45-revision-length",
        min_chars=38,
        max_chars=42,
    )
    assert initial.manifest.length_check is not None
    assert initial.manifest.length_check.actual_chars == 40
    _publish_audit(
        tmp_path,
        workspace,
        context_id="length-audit-context-1",
        material=True,
    )

    with pytest.raises(VepError) as error:
        _publish_revision(tmp_path, workspace, context_id="length-revision-context-1")

    assert error.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    assert not workspace.safe_path("manifests/authoring/draft-v002.json").exists()
    assert initial.manifest.semantic_revision_count == 0
    assert initial.manifest.length_adjustment_count == 0


def test_initial_length_exhaustion_is_durable_blocked_and_recoverable(tmp_path: Path) -> None:
    workspace = _evidence_ready_job(
        tmp_path,
        "length-initial-block",
        min_chars=50,
        max_chars=55,
        maximum_length_adjustments=1,
    )
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    first = _author_response(request)
    first_path = tmp_path / "length-initial-first.json"
    _write_model(first_path, first)
    with pytest.raises(VepError) as required:
        _import_authoring_fixture(workspace.root, first_path)
    assert required.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    adjustment_request = json.loads(
        workspace.safe_path("authoring/length/v001-request.json").read_text(encoding="utf-8")
    )
    assert adjustment_request["adjustment_number"] == 1
    assert adjustment_request["adjustments_remaining_after_this"] == 0
    assert adjustment_request["candidate_artifact"]["artifact_type"] == (
        "failed_length_candidate_response"
    )

    second = _author_response(request, context_id="author-context-2")
    second_path = tmp_path / "length-initial-second.json"
    _write_model(second_path, second)
    with pytest.raises(VepError) as exhausted:
        _import_authoring_fixture(workspace.root, second_path)
    assert exhausted.value.code == "LENGTH_ADJUSTMENT_EXHAUSTED"
    request_path = workspace.safe_path("authoring/length/v001-request.json")
    request_bytes = request_path.read_bytes()
    request_path.unlink()
    with pytest.raises(VepError) as missing_request:
        validate_job(workspace.root)
    assert missing_request.value.code == "LENGTH_ADJUSTMENT_REQUEST_INVALID"
    assert not request_path.exists()
    request_path.write_bytes(request_bytes)
    attempts_path = workspace.safe_path("authoring/length/v001-attempts.jsonl")
    attempts_bytes = attempts_path.read_bytes()
    attempts_path.write_bytes(attempts_bytes.splitlines(keepends=True)[0])
    with pytest.raises(VepError) as truncated_ledger:
        validate_job(workspace.root, deep=True)
    assert truncated_ledger.value.code == "LENGTH_BLOCK_INVALID"
    attempts_path.write_bytes(attempts_bytes)
    assert validate_job(workspace.root)["state"] == JobState.BLOCKED.value
    assert validate_job(workspace.root, deep=True)["state"] == JobState.BLOCKED.value
    block = load_length_block(workspace, load_job_yaml(workspace.job_yaml), deep=True)
    assert block.blocked_stage == "initial_authoring"
    assert block.maximum_adjustments == 1
    assert block.final_actual_chars == len(second.draft_markdown.strip())
    assert block.final_candidate_artifact.artifact_type == "failed_length_candidate_response"
    assert not workspace.safe_path("manifests/authoring/draft-v001.json").exists()
    with pytest.raises(VepError) as terminal_retry:
        _import_authoring_fixture(workspace.root, second_path)
    assert terminal_retry.value.code == "LENGTH_EXHAUSTION_BLOCKED"

    transition_bytes = workspace.transitions_jsonl.read_bytes()
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    transitions[-1] = transitions[-1].model_copy(update={"trigger": "revision.length-exhausted"})
    workspace.transitions_jsonl.write_text(
        "".join(item.model_dump_json() + "\n" for item in transitions),
        encoding="utf-8",
    )
    with pytest.raises(VepError) as wrong_reason:
        validate_job(workspace.root)
    assert wrong_reason.value.code == "LENGTH_BLOCK_INVALID"
    workspace.transitions_jsonl.write_bytes(transition_bytes)

    child = recover_job(
        workspace.root,
        "length-initial-child",
        confirm=True,
        basis="recover from durable initial length exhaustion",
    ).workspace
    assert validate_job(child.root)["state"] == JobState.TRANSCRIBED.value
    assert validate_job(child.root, deep=True)["state"] == JobState.TRANSCRIBED.value


def test_successful_length_adjustment_ledger_is_required_by_deep_validation(
    tmp_path: Path,
) -> None:
    workspace = _evidence_ready_job(
        tmp_path,
        "length-success-ledger",
        min_chars=50,
        max_chars=55,
        maximum_length_adjustments=2,
    )
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    first = _author_response(request)
    first_path = tmp_path / "length-success-first.json"
    _write_model(first_path, first)
    with pytest.raises(VepError) as required:
        _import_authoring_fixture(workspace.root, first_path)
    assert required.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    second = _author_response(request, context_id="length-success-context-2").model_copy(
        update={"draft_markdown": first.draft_markdown + "123456789012"}
    )
    second_path = tmp_path / "length-success-second.json"
    _write_model(second_path, second)
    published = _import_authoring_fixture(workspace.root, second_path)
    assert published.manifest.length_adjustment_count == 1
    assert validate_job(workspace.root, deep=True)["state"] == JobState.DRAFTED.value

    attempts_path = workspace.safe_path("authoring/length/v001-attempts.jsonl")
    request_path = workspace.safe_path("authoring/length/v001-request.json")
    candidate = next(workspace.safe_path("authoring/length/v001").rglob("candidate-response.json"))
    attempts_path.unlink()
    request_path.unlink()
    candidate.unlink()
    with pytest.raises(VepError) as missing_ledger:
        validate_job(workspace.root, deep=True)
    assert missing_ledger.value.code == "DRAFT_BUNDLE_INVALID"


def test_revision_length_exhaustion_blocks_without_spending_semantic_budget(
    tmp_path: Path,
) -> None:
    workspace, initial = _publish_initial_draft(
        tmp_path,
        "length-revision-block",
        min_chars=38,
        max_chars=42,
        maximum_length_adjustments=1,
    )
    _publish_audit(
        tmp_path,
        workspace,
        context_id="length-block-audit",
        material=True,
    )
    request = prepare_revision_job(workspace.root, "revise-from-audit").request
    first = _revision_response(request, context_id="length-block-revision-1")
    first_path = tmp_path / "length-revision-first.json"
    _write_model(first_path, first)
    with pytest.raises(VepError) as required:
        _import_revision_fixture(workspace.root, first_path)
    assert required.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    assert validate_job(workspace.root)["state"] == JobState.REVISION_REQUIRED.value

    second = _revision_response(request, context_id="length-block-revision-2")
    second_path = tmp_path / "length-revision-second.json"
    _write_model(second_path, second)
    with pytest.raises(VepError) as exhausted:
        _import_revision_fixture(workspace.root, second_path)
    assert exhausted.value.code == "LENGTH_ADJUSTMENT_EXHAUSTED"
    assert validate_job(workspace.root)["state"] == JobState.BLOCKED.value
    assert validate_job(workspace.root, deep=True)["state"] == JobState.BLOCKED.value
    block = load_length_block(workspace, load_job_yaml(workspace.job_yaml), deep=True)
    assert block.blocked_stage == "semantic_revision"
    assert block.target_draft_version == "v002"
    assert initial.manifest.semantic_revision_count == 0
    assert not workspace.safe_path("manifests/authoring/draft-v002.json").exists()


@pytest.mark.parametrize("mode", ["initial", "revision"])
@pytest.mark.parametrize(
    ("crash_boundary", "retry_same"),
    [
        ("candidate", False),
        ("candidate", True),
        ("ledger", False),
        ("ledger", True),
        ("request", False),
        ("request", True),
        ("block", True),
    ],
)
def test_length_publication_crash_matrix_recovers_by_normal_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    crash_boundary: str,
    retry_same: bool,
) -> None:
    maximum = 1 if crash_boundary == "block" else 2
    if mode == "initial":
        workspace = _evidence_ready_job(
            tmp_path,
            f"length-crash-{mode}-{crash_boundary}",
            min_chars=50,
            max_chars=55,
            maximum_length_adjustments=maximum,
        )
        request = prepare_authoring_job(workspace.root, "draft-video-report").request
        first = _author_response(request, context_id=f"{mode}-{crash_boundary}-first")
        corrected = first.model_copy(
            update={
                "author": _identity(AgentRole.AUTHOR, f"{mode}-{crash_boundary}-corrected"),
                "draft_markdown": first.draft_markdown + "123456789012",
                "created_at": datetime.now(UTC),
            }
        )
        importer = _import_authoring_fixture
        version = "v001"
    else:
        workspace, _initial = _publish_initial_draft(
            tmp_path,
            f"length-crash-{mode}-{crash_boundary}",
            min_chars=38,
            max_chars=42,
            maximum_length_adjustments=maximum,
        )
        _publish_audit(
            tmp_path,
            workspace,
            context_id=f"{mode}-{crash_boundary}-audit",
            material=True,
        )
        request = prepare_revision_job(workspace.root, "revise-from-audit").request
        first = _revision_response(request, context_id=f"{mode}-{crash_boundary}-first")
        corrected = _revision_response(
            request,
            context_id=f"{mode}-{crash_boundary}-corrected",
            replacement_suffix="xxx",
        )
        importer = _import_revision_fixture
        version = "v002"

    first_path = tmp_path / f"{mode}-{crash_boundary}-first.json"
    corrected_path = tmp_path / f"{mode}-{crash_boundary}-corrected.json"
    _write_model(first_path, first)
    _write_model(corrected_path, corrected)

    if crash_boundary == "block":
        with pytest.raises(VepError) as required:
            importer(workspace.root, first_path)
        assert required.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
        exhausted = first.model_copy(
            update={
                ("author" if mode == "initial" else "reviser"): _identity(
                    AgentRole.AUTHOR if mode == "initial" else AgentRole.REVISER,
                    f"{mode}-{crash_boundary}-exhausted",
                ),
                "created_at": datetime.now(UTC),
            }
        )
        exhausted_path = tmp_path / f"{mode}-{crash_boundary}-exhausted.json"
        _write_model(exhausted_path, exhausted)
        original_transition = application_module._append_transition_locked
        crashed = False

        def crash_before_block_transition(*args, **kwargs):
            nonlocal crashed
            if kwargs.get("trigger", "").endswith("length-exhausted") and not crashed:
                crashed = True
                raise RuntimeError("simulated crash after length-block publication")
            return original_transition(*args, **kwargs)

        monkeypatch.setattr(
            application_module, "_append_transition_locked", crash_before_block_transition
        )
        with pytest.raises(RuntimeError, match="after length-block publication"):
            importer(workspace.root, exhausted_path)
        block_path = workspace.safe_path("manifests/authoring/length-block.json")
        block_bytes = block_path.read_bytes()
        monkeypatch.setattr(application_module, "_append_transition_locked", original_transition)
        with pytest.raises(VepError) as retried:
            importer(workspace.root, exhausted_path)
        assert retried.value.code == "LENGTH_ADJUSTMENT_EXHAUSTED"
        assert block_path.read_bytes() == block_bytes
        assert validate_job(workspace.root)["state"] == JobState.BLOCKED.value
        assert validate_job(workspace.root, deep=True)["state"] == JobState.BLOCKED.value
        assert read_jsonl(workspace.transitions_jsonl, TransitionRecord)[-1].trigger == (
            "author.length-exhausted" if mode == "initial" else "revision.length-exhausted"
        )
        return

    if crash_boundary == "candidate":
        original = authoring_pipeline_module.append_jsonl

        def crash_before_ledger(path, value):
            if path.name.endswith("-attempts.jsonl"):
                raise RuntimeError("simulated crash after candidate publication")
            return original(path, value)

        monkeypatch.setattr(authoring_pipeline_module, "append_jsonl", crash_before_ledger)
    else:
        original = authoring_pipeline_module._write_machine_json

        def crash_at_request(path, value, *, code):
            if path.name.endswith("-request.json"):
                if crash_boundary == "request":
                    original(path, value, code=code)
                raise RuntimeError(f"simulated crash after {crash_boundary} publication")
            return original(path, value, code=code)

        monkeypatch.setattr(authoring_pipeline_module, "_write_machine_json", crash_at_request)

    with pytest.raises(RuntimeError, match="simulated crash"):
        importer(workspace.root, first_path)
    if crash_boundary == "candidate":
        monkeypatch.setattr(authoring_pipeline_module, "append_jsonl", original)
    else:
        monkeypatch.setattr(authoring_pipeline_module, "_write_machine_json", original)

    if retry_same:
        with pytest.raises(VepError) as same_retry:
            importer(workspace.root, first_path)
        assert same_retry.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    published = importer(workspace.root, corrected_path)
    draft = published if mode == "initial" else published.draft
    assert draft.manifest.length_adjustment_count == 1
    assert draft.manifest.semantic_revision_count == (0 if mode == "initial" else 1)
    assert workspace.safe_path(f"authoring/length/{version}-attempts.jsonl").is_file()
    request_path = workspace.safe_path(f"authoring/length/{version}-request.json")
    assert request_path.is_file()
    request_bytes = request_path.read_bytes()
    assert (
        len(
            list(
                workspace.safe_path(f"authoring/length/{version}").rglob("candidate-response.json")
            )
        )
        == 1
    )
    monkeypatch.setattr(
        authoring_pipeline_module,
        "_write_machine_json",
        lambda *_args, **_kwargs: pytest.fail("job validation must be read-only"),
    )
    assert validate_job(workspace.root)["state"] == JobState.DRAFTED.value
    assert validate_job(workspace.root, deep=True)["state"] == JobState.DRAFTED.value
    assert request_path.read_bytes() == request_bytes


def test_second_revision_material_audit_blocks_without_v004(tmp_path: Path) -> None:
    workspace, _initial = _publish_initial_draft(tmp_path, "issue15-revision-block")
    _publish_audit(
        tmp_path,
        workspace,
        context_id="audit-context-1",
        material=True,
    )
    first_revision = _publish_revision(
        tmp_path,
        workspace,
        context_id="revision-context-1",
    )
    assert first_revision.draft.manifest.draft_version == "v002"
    _publish_audit(
        tmp_path,
        workspace,
        context_id="audit-context-2",
        material=True,
    )
    second_revision = _publish_revision(
        tmp_path,
        workspace,
        context_id="revision-context-2",
    )
    assert second_revision.draft.manifest.draft_version == "v003"
    assert second_revision.draft.manifest.revision_count == 2
    blocked = _publish_audit(
        tmp_path,
        workspace,
        context_id="audit-context-3",
        material=True,
    )
    assert blocked.manifest.disposition.value == "blocked"
    assert blocked.human_review is None
    assert validate_job(workspace.root)["state"] == JobState.BLOCKED.value
    assert not workspace.safe_path("manifests/authoring/draft-v004.json").exists()
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in transitions[-9:]] == [
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.BLOCKED,
    ]
    workspace.safe_path("manifests/authoring/length-block.json").write_text("{}", encoding="utf-8")
    assert validate_job(workspace.root)["state"] == JobState.BLOCKED.value
    assert validate_job(workspace.root, deep=True)["state"] == JobState.BLOCKED.value
    child = recover_job(
        workspace.root,
        "audit-block-with-stray-length-child",
        confirm=True,
        basis="audit blocked recovery ignores a stray length artifact",
    ).workspace
    assert validate_job(child.root)["state"] == JobState.TRANSCRIBED.value


def test_revision_resolution_hunks_and_evidence_bind_to_the_matching_finding(
    tmp_path: Path,
) -> None:
    workspace, _draft = _publish_initial_draft(tmp_path, "issue15-finding-lineage")
    audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    first = _audit_response(audit_request, material=True).findings[0]
    second_text = "key event"
    second_start = audit_request.target_draft_markdown.index(second_text)
    second = AuditFinding(
        finding_id="AF000002",
        severity=AuditSeverity.P1,
        category="requirement-coverage",
        rationale="The separately anchored requirement needs a bounded revision.",
        draft_span=DraftSpan(
            start=second_start,
            end=second_start + len(second_text),
            text=second_text,
        ),
        requirement_ids=["R000002"],
        evidence_ids=["E000001"],
        source_refs=list(audit_request.fixed_evidence.evidence_records[0].source_refs),
        confidence=ClaimConfidence.HIGH,
    )
    two_findings = _audit_response(audit_request, material=True).model_copy(
        update={"findings": [first, second]}
    )
    audit_path = tmp_path / "finding-lineage-audit.json"
    _write_model(audit_path, two_findings)
    _import_audit_fixture(workspace.root, audit_path)
    revision_request = prepare_revision_job(workspace.root, "revise-from-audit").request

    replacements = {
        finding.finding_id: f"[FIX-{index}]"
        for index, finding in enumerate(revision_request.audit_findings, start=1)
    }
    revised = revision_request.target_draft_markdown
    for finding in sorted(
        revision_request.audit_findings,
        key=lambda item: item.draft_span.start if item.draft_span is not None else -1,
        reverse=True,
    ):
        assert finding.draft_span is not None
        anchor = finding.draft_span
        revised = revised[: anchor.start] + replacements[finding.finding_id] + revised[anchor.end :]
    claim_text = replacements["AF000001"]
    claim_start = revised.index(claim_text)
    claim = ClaimRecord(
        claim_id="C000001",
        draft_span=DraftSpan(
            start=claim_start,
            end=claim_start + len(claim_text),
            text=claim_text,
        ),
        text=claim_text,
        claim_type=ClaimType.VIDEO_FACT,
        evidence_ids=["E000001"],
        requirement_ids=[
            item.requirement_id for item in revision_request.output_constraints.requirements
        ],
        author_confidence=ClaimConfidence.HIGH,
    )
    changed_by_finding: dict[str, list[DraftSpan]] = {
        finding.finding_id: [] for finding in revision_request.audit_findings
    }
    removed_by_finding: dict[str, list[DraftSpan]] = {
        finding.finding_id: [] for finding in revision_request.audit_findings
    }
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None,
        revision_request.target_draft_markdown,
        revised,
        autojunk=False,
    ).get_opcodes():
        if tag == "equal":
            continue
        owners = [
            finding
            for finding in revision_request.audit_findings
            if finding.draft_span is not None
            and old_start <= finding.draft_span.end
            and finding.draft_span.start <= old_end
        ]
        assert len(owners) == 1
        owner = owners[0].finding_id
        if new_start < new_end:
            changed_by_finding[owner].append(
                DraftSpan(start=new_start, end=new_end, text=revised[new_start:new_end])
            )
        if old_start < old_end:
            removed_by_finding[owner].append(
                DraftSpan(
                    start=old_start,
                    end=old_end,
                    text=revision_request.target_draft_markdown[old_start:old_end],
                )
            )
    resolutions = [
        ResolutionRecord(
            finding_id=finding.finding_id,
            decision=ResolutionDecision.ACCEPT,
            resolution="The exact anchored hunk resolves this finding.",
            changed_spans=changed_by_finding[finding.finding_id],
            removed_spans=removed_by_finding[finding.finding_id],
            evidence_ids=finding.evidence_ids,
        )
        for finding in revision_request.audit_findings
    ]
    valid = RevisionResponseManifest(
        request_fingerprint=revision_request.request_fingerprint,
        reviser=_identity(AgentRole.REVISER, "finding-lineage-reviser"),
        draft_markdown=revised,
        claims=[claim],
        requirement_map=_requirement_map(revision_request, claim.claim_id, "E000001"),
        resolutions=resolutions,
        created_at=datetime.now(UTC),
    )
    left, right = valid.resolutions
    swapped = valid.model_copy(
        update={
            "resolutions": [
                left.model_copy(
                    update={
                        "changed_spans": right.changed_spans,
                        "removed_spans": right.removed_spans,
                    }
                ),
                right.model_copy(
                    update={
                        "changed_spans": left.changed_spans,
                        "removed_spans": left.removed_spans,
                    }
                ),
            ]
        }
    )
    swapped_path = tmp_path / "finding-lineage-swapped.json"
    _write_model(swapped_path, swapped)
    with pytest.raises(VepError) as swapped_error:
        _import_revision_fixture(workspace.root, swapped_path)
    assert swapped_error.value.code == "REVISION_RESPONSE_INVALID"

    empty_evidence = valid.model_copy(
        update={
            "resolutions": [
                left.model_copy(update={"evidence_ids": []}),
                right,
            ]
        }
    )
    empty_evidence_path = tmp_path / "finding-lineage-empty-evidence.json"
    _write_model(empty_evidence_path, empty_evidence)
    with pytest.raises(VepError) as evidence_error:
        _import_revision_fixture(workspace.root, empty_evidence_path)
    assert evidence_error.value.code == "REVISION_RESPONSE_INVALID"

    valid_path = tmp_path / "finding-lineage-valid.json"
    _write_model(valid_path, valid)
    assert (
        _import_revision_fixture(workspace.root, valid_path).draft.manifest.draft_version == "v002"
    )


def test_overlapping_finding_anchors_may_share_the_same_revision_hunk(tmp_path: Path) -> None:
    workspace, _draft = _publish_initial_draft(tmp_path, "issue15-shared-finding-hunk")
    audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    first = _audit_response(audit_request, material=True).findings[0]
    second = first.model_copy(
        update={
            "finding_id": "AF000002",
            "category": "requirement-coverage",
            "rationale": "The same exact anchor affects a second requirement.",
            "requirement_ids": ["R000002"],
        }
    )
    shared_audit = _audit_response(audit_request, material=True).model_copy(
        update={"findings": [first, second]}
    )
    audit_path = tmp_path / "shared-finding-hunk-audit.json"
    _write_model(audit_path, shared_audit)
    _import_audit_fixture(workspace.root, audit_path)
    request = prepare_revision_job(workspace.root, "revise-from-audit").request
    response_path = tmp_path / "shared-finding-hunk-revision.json"
    _write_model(
        response_path,
        _revision_response(request, context_id="shared-finding-reviser"),
    )
    assert (
        _import_revision_fixture(workspace.root, response_path).draft.manifest.draft_version
        == "v002"
    )


def test_authoring_crash_recovery_and_concurrent_audit_have_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _evidence_ready_job(tmp_path, "issue15-recovery-lock")
    author_request = prepare_authoring_job(workspace.root, "draft-video-report").request
    author_path = tmp_path / "recovery-author.json"
    _write_model(author_path, _author_response(author_request))

    original_append = application_module._append_transition_locked
    crashed = False

    def crash_after_publish(*args, **kwargs):
        nonlocal crashed
        if kwargs.get("trigger") == "author.import" and not crashed:
            crashed = True
            raise RuntimeError("synthetic transition interruption")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(application_module, "_append_transition_locked", crash_after_publish)
    with pytest.raises(RuntimeError, match="synthetic transition interruption"):
        _import_authoring_fixture(workspace.root, author_path)
    assert workspace.safe_path("manifests/authoring/draft-v001.json").is_file()
    assert (
        read_jsonl(workspace.transitions_jsonl, TransitionRecord)[-1].to_state
        is JobState.EVIDENCE_READY
    )

    monkeypatch.setattr(application_module, "_append_transition_locked", original_append)
    recovered = _import_authoring_fixture(workspace.root, author_path)
    assert recovered.reused is True
    staging = workspace.safe_path("semantic/staging")
    assert list(staging.iterdir()) == []

    audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    audit_path = tmp_path / "concurrent-audit.json"
    _write_model(audit_path, _audit_response(audit_request))
    original_stage = application_module.stage_audit
    stage_started = threading.Event()
    release_stage = threading.Event()

    def slow_stage(*args, **kwargs):
        stage_started.set()
        assert release_stage.wait(timeout=10)
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(application_module, "stage_audit", slow_stage)
    results: list[object] = []
    failures: list[BaseException] = []

    def run_winner() -> None:
        try:
            results.append(_import_audit_fixture(workspace.root, audit_path))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    winner = threading.Thread(target=run_winner, daemon=True)
    winner.start()
    assert stage_started.wait(timeout=10)
    with pytest.raises(VepError) as busy_error:
        _import_audit_fixture(workspace.root, audit_path)
    assert busy_error.value.code == "SEMANTIC_STAGE_BUSY"
    release_stage.set()
    winner.join(timeout=15)
    assert not winner.is_alive()
    assert not failures
    assert len(results) == 1
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in transitions].count(JobState.DRAFTED) == 1
    assert [item.to_state for item in transitions].count(JobState.AUDITED) == 1
    assert [item.to_state for item in transitions].count(JobState.HUMAN_REVIEW_READY) == 1


def test_primary_manifest_crash_recovery_reuses_complete_author_audit_and_revision_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = authoring_pipeline_module._write_or_verify_model

    def crash_once_at(target: Path):
        crashed = False

        def crash(path, *args, **kwargs):
            nonlocal crashed
            if path == target and not crashed:
                crashed = True
                raise RuntimeError("synthetic primary manifest interruption")
            return original_write(path, *args, **kwargs)

        return crash

    author_tmp = tmp_path / "author"
    author_tmp.mkdir()
    author_workspace = _evidence_ready_job(author_tmp, "issue15-recover-author-primary")
    author_request = prepare_authoring_job(author_workspace.root, "draft-video-report").request
    author_path = author_tmp / "recover-author-primary.json"
    _write_model(author_path, _author_response(author_request))
    author_primary = author_workspace.safe_path("manifests/authoring/draft-v001.json")
    monkeypatch.setattr(
        authoring_pipeline_module,
        "_write_or_verify_model",
        crash_once_at(author_primary),
    )
    with pytest.raises(RuntimeError, match="primary manifest interruption"):
        _import_authoring_fixture(author_workspace.root, author_path)
    assert not author_primary.exists()
    assert author_workspace.safe_path("authoring/drafts/v001/agent-run.json").is_file()
    monkeypatch.setattr(authoring_pipeline_module, "_write_or_verify_model", original_write)
    assert _import_authoring_fixture(author_workspace.root, author_path).manifest.created_at
    author_transitions = read_jsonl(author_workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in author_transitions].count(JobState.DRAFTED) == 1

    audit_tmp = tmp_path / "audit"
    audit_tmp.mkdir()
    audit_workspace, _draft = _publish_initial_draft(audit_tmp, "issue15-recover-audit-primary")
    audit_request = prepare_audit_job(audit_workspace.root, "independent-content-audit").request
    audit_path = audit_tmp / "recover-audit-primary.json"
    _write_model(audit_path, _audit_response(audit_request, material=True))
    audit_primary = audit_workspace.safe_path("manifests/authoring/audit-v001.json")
    monkeypatch.setattr(
        authoring_pipeline_module,
        "_write_or_verify_model",
        crash_once_at(audit_primary),
    )
    with pytest.raises(RuntimeError, match="primary manifest interruption"):
        _import_audit_fixture(audit_workspace.root, audit_path)
    assert not audit_primary.exists()
    assert audit_workspace.safe_path("authoring/audits/v001/agent-run.json").is_file()
    monkeypatch.setattr(authoring_pipeline_module, "_write_or_verify_model", original_write)
    recovered_audit = _import_audit_fixture(audit_workspace.root, audit_path)
    assert recovered_audit.manifest.created_at == recovered_audit.agent_run.finished_at
    audit_transitions = read_jsonl(audit_workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in audit_transitions].count(JobState.AUDITED) == 1
    assert [item.to_state for item in audit_transitions].count(JobState.REVISION_REQUIRED) == 1

    revision_tmp = tmp_path / "revision"
    revision_tmp.mkdir()
    revision_workspace, _draft = _publish_initial_draft(
        revision_tmp, "issue15-recover-revision-primary"
    )
    _publish_audit(
        revision_tmp,
        revision_workspace,
        context_id="recover-revision-audit",
        material=True,
    )
    revision_request = prepare_revision_job(revision_workspace.root, "revise-from-audit").request
    revision_path = revision_tmp / "recover-revision-primary.json"
    _write_model(
        revision_path,
        _revision_response(revision_request, context_id="recover-revision-context"),
    )
    revision_draft_primary = revision_workspace.safe_path("manifests/authoring/draft-v002.json")
    revision_manifest = revision_workspace.safe_path("manifests/authoring/revision-v002.json")
    monkeypatch.setattr(
        authoring_pipeline_module,
        "_write_or_verify_model",
        crash_once_at(revision_draft_primary),
    )
    with pytest.raises(RuntimeError, match="primary manifest interruption"):
        _import_revision_fixture(revision_workspace.root, revision_path)
    assert revision_manifest.is_file()
    assert not revision_draft_primary.exists()
    monkeypatch.setattr(authoring_pipeline_module, "_write_or_verify_model", original_write)
    recovered_revision = _import_revision_fixture(revision_workspace.root, revision_path)
    assert recovered_revision.manifest.created_at == recovered_revision.draft.agent_run.finished_at
    revision_transitions = read_jsonl(revision_workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in revision_transitions].count(JobState.DRAFTED) == 2


def test_human_review_bundle_crash_recovery_is_deterministic_and_rejects_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = authoring_pipeline_module._write_or_verify_model

    def crash_once_at(target: Path):
        crashed = False

        def crash(path, *args, **kwargs):
            nonlocal crashed
            if path == target and not crashed:
                crashed = True
                raise RuntimeError("synthetic human-review manifest interruption")
            return original_write(path, *args, **kwargs)

        return crash

    recover_root = tmp_path / "recover"
    recover_root.mkdir()
    workspace, _draft = _publish_initial_draft(recover_root, "issue15-human-review-recovery")
    request = prepare_audit_job(workspace.root, "independent-content-audit").request
    response_path = recover_root / "human-review-recovery-audit.json"
    _write_model(response_path, _audit_response(request, material=False))
    stage_path = workspace.safe_path("manifests/stages/human-review-v001.json")
    bundle_path = workspace.safe_path("human-review/v001/bundle.json")
    monkeypatch.setattr(
        authoring_pipeline_module,
        "_write_or_verify_model",
        crash_once_at(stage_path),
    )
    with pytest.raises(RuntimeError, match="human-review manifest interruption"):
        _import_audit_fixture(workspace.root, response_path)
    assert bundle_path.is_file()
    assert not stage_path.exists()
    assert not workspace.human_review_manifest_json.exists()
    monkeypatch.setattr(authoring_pipeline_module, "_write_or_verify_model", original_write)

    recovered = _import_audit_fixture(workspace.root, response_path)
    assert recovered.human_review is not None
    assert (
        json.loads(bundle_path.read_text(encoding="utf-8"))["created_at"]
        == json.loads(workspace.human_review_manifest_json.read_text(encoding="utf-8"))[
            "created_at"
        ]
    )
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in transitions].count(JobState.HUMAN_REVIEW_READY) == 1
    assert validate_job(workspace.root)["state"] == JobState.HUMAN_REVIEW_READY.value

    mismatch_root = tmp_path / "m"
    mismatch_root.mkdir()
    mismatch_workspace, _draft = _publish_initial_draft(
        mismatch_root, "issue15-human-review-orphan-mismatch"
    )
    mismatch_request = prepare_audit_job(
        mismatch_workspace.root, "independent-content-audit"
    ).request
    mismatch_response_path = mismatch_root / "human-review-mismatch-audit.json"
    _write_model(mismatch_response_path, _audit_response(mismatch_request, material=False))
    mismatch_stage_path = mismatch_workspace.safe_path("manifests/stages/human-review-v001.json")
    monkeypatch.setattr(
        authoring_pipeline_module,
        "_write_or_verify_model",
        crash_once_at(mismatch_stage_path),
    )
    with pytest.raises(RuntimeError, match="human-review manifest interruption"):
        _import_audit_fixture(mismatch_workspace.root, mismatch_response_path)
    mismatch_bundle_path = mismatch_workspace.safe_path("human-review/v001/bundle.json")
    payload = json.loads(mismatch_bundle_path.read_text(encoding="utf-8"))
    payload["p2_count"] = 1
    _write_model(mismatch_bundle_path, payload)
    monkeypatch.setattr(authoring_pipeline_module, "_write_or_verify_model", original_write)
    with pytest.raises(VepError) as mismatch_error:
        _import_audit_fixture(mismatch_workspace.root, mismatch_response_path)
    assert mismatch_error.value.code == "HUMAN_REVIEW_ORPHAN_INVALID"
    mismatch_transitions = read_jsonl(mismatch_workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in mismatch_transitions].count(JobState.HUMAN_REVIEW_READY) == 0


def test_completed_production_replays_accept_frozen_skill_snapshots_after_drift(
    tmp_path: Path,
) -> None:
    author_root = tmp_path / "author"
    author_root.mkdir()
    author_workspace = _evidence_ready_job(author_root, "issue15-author-frozen-replay")
    author_request = prepare_authoring_job(author_workspace.root, "draft-video-report").request
    author_path = author_root / "author-replay.json"
    author_response = _author_response(
        author_request,
        context_id="frozen-author",
        execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
    )
    _write_model(author_path, author_response)
    _set_skill_lifecycle(author_workspace, "draft-video-report", "active")
    assert import_authoring_job(author_workspace.root, author_path).reused is False
    _mutate_skill_content(author_workspace, "draft-video-report")
    assert import_authoring_job(author_workspace.root, author_path).reused is True
    mismatch_author_path = author_root / "author-replay-mismatch.json"
    _write_model(
        mismatch_author_path,
        author_response.model_copy(update={"created_at": datetime.now(UTC)}),
    )
    with pytest.raises(VepError) as author_mismatch:
        import_authoring_job(author_workspace.root, mismatch_author_path)
    assert author_mismatch.value.code == "AUTHORING_FINGERPRINT_MISMATCH"

    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    audit_workspace = _evidence_ready_job(audit_root, "issue15-audit-frozen-replay")
    audit_author_request = prepare_authoring_job(audit_workspace.root, "draft-video-report").request
    audit_author_path = audit_root / "audit-author.json"
    _write_model(
        audit_author_path,
        _author_response(
            audit_author_request,
            context_id="frozen-audit-author",
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    _set_skill_lifecycle(audit_workspace, "draft-video-report", "active")
    import_authoring_job(audit_workspace.root, audit_author_path)
    audit_request = prepare_audit_job(audit_workspace.root, "independent-content-audit").request
    audit_path = audit_root / "audit-replay.json"
    _write_model(
        audit_path,
        _audit_response(
            audit_request,
            context_id="frozen-audit",
            material=True,
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    _set_skill_lifecycle(audit_workspace, "independent-content-audit", "active")
    assert import_audit_job(audit_workspace.root, audit_path).reused is False
    _mutate_skill_evaluation(audit_workspace, "independent-content-audit")
    assert import_audit_job(audit_workspace.root, audit_path).reused is True

    revision_root = tmp_path / "revision"
    revision_root.mkdir()
    revision_workspace = _evidence_ready_job(revision_root, "issue15-revision-frozen-replay")
    revision_author_request = prepare_authoring_job(
        revision_workspace.root, "draft-video-report"
    ).request
    revision_author_path = revision_root / "revision-author.json"
    _write_model(
        revision_author_path,
        _author_response(
            revision_author_request,
            context_id="frozen-revision-author",
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    _set_skill_lifecycle(revision_workspace, "draft-video-report", "active")
    import_authoring_job(revision_workspace.root, revision_author_path)
    revision_audit_request = prepare_audit_job(
        revision_workspace.root, "independent-content-audit"
    ).request
    revision_audit_path = revision_root / "revision-audit.json"
    _write_model(
        revision_audit_path,
        _audit_response(
            revision_audit_request,
            context_id="frozen-revision-audit",
            material=True,
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
    )
    _set_skill_lifecycle(revision_workspace, "independent-content-audit", "active")
    import_audit_job(revision_workspace.root, revision_audit_path)
    revision_request = prepare_revision_job(revision_workspace.root, "revise-from-audit").request
    revision_path = revision_root / "revision-replay.json"
    revision_response = _revision_response(
        revision_request,
        context_id="frozen-revision",
    ).model_copy(
        update={
            "reviser": _identity(
                AgentRole.REVISER,
                "frozen-revision",
                execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
            )
        }
    )
    _write_model(revision_path, revision_response)
    _set_skill_lifecycle(revision_workspace, "revise-from-audit", "active")
    assert import_revision_job(revision_workspace.root, revision_path).reused is False
    _mutate_skill_content(revision_workspace, "revise-from-audit")
    _mutate_skill_evaluation(revision_workspace, "revise-from-audit")
    assert import_revision_job(revision_workspace.root, revision_path).reused is True


def _insertion_revision_response(request, insertion_at: int) -> RevisionResponseManifest:
    marker = f"[INSERT-{insertion_at}]"
    draft = (
        request.target_draft_markdown[:insertion_at]
        + marker
        + request.target_draft_markdown[insertion_at:]
    )
    claim_text = "application evidence"
    claim_start = draft.index(claim_text)
    claim = ClaimRecord(
        claim_id="C000001",
        draft_span=DraftSpan(
            start=claim_start,
            end=claim_start + len(claim_text),
            text=claim_text,
        ),
        text=claim_text,
        claim_type=ClaimType.VIDEO_FACT,
        evidence_ids=["E000001"],
        requirement_ids=[item.requirement_id for item in request.output_constraints.requirements],
        author_confidence=ClaimConfidence.HIGH,
    )
    changed = DraftSpan(start=insertion_at, end=insertion_at + len(marker), text=marker)
    return RevisionResponseManifest(
        request_fingerprint=request.request_fingerprint,
        reviser=_identity(AgentRole.REVISER, f"insertion-{insertion_at}"),
        draft_markdown=draft,
        claims=[claim],
        requirement_map=_requirement_map(request, claim.claim_id, "E000001"),
        resolutions=[
            ResolutionRecord(
                finding_id=finding.finding_id,
                decision=ResolutionDecision.ACCEPT,
                resolution="The inserted qualification resolves the exact finding.",
                changed_spans=[changed],
                evidence_ids=finding.evidence_ids,
            )
            for finding in request.audit_findings
        ],
        created_at=datetime.now(UTC),
    )


def test_revision_hunk_attribution_rejects_adjacent_edits_and_allows_shared_insertion(
    tmp_path: Path,
) -> None:
    workspace, _draft = _publish_initial_draft(tmp_path, "issue15-hunk-boundaries")
    audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    first = _audit_response(audit_request, material=True).findings[0]
    second = first.model_copy(
        update={
            "finding_id": "AF000002",
            "category": "requirement-coverage",
            "rationale": "The overlapping anchor affects a second requirement.",
            "requirement_ids": ["R000002"],
        }
    )
    audit_path = tmp_path / "hunk-boundary-audit.json"
    _write_model(
        audit_path,
        _audit_response(audit_request, material=True).model_copy(
            update={"findings": [first, second]}
        ),
    )
    _import_audit_fixture(workspace.root, audit_path)
    request = prepare_revision_job(workspace.root, "revise-from-audit").request
    anchor = request.audit_findings[0].draft_span
    assert anchor is not None

    for label, insertion_at in (("left", anchor.start - 1), ("right", anchor.end + 1)):
        adjacent_path = tmp_path / f"hunk-boundary-{label}.json"
        _write_model(adjacent_path, _insertion_revision_response(request, insertion_at))
        with pytest.raises(VepError) as adjacent_error:
            _import_revision_fixture(workspace.root, adjacent_path)
        assert adjacent_error.value.code == "REVISION_RESPONSE_INVALID"

    shared_path = tmp_path / "hunk-boundary-shared-insertion.json"
    _write_model(shared_path, _insertion_revision_response(request, anchor.start))
    assert (
        _import_revision_fixture(workspace.root, shared_path).draft.manifest.draft_version == "v002"
    )


def test_revision_hunk_attribution_preserves_changed_and_removed_sides_on_tuple_collision(
    tmp_path: Path,
) -> None:
    workspace, _draft = _publish_initial_draft(tmp_path, "issue15-hunk-side-collision")
    _publish_audit(
        tmp_path,
        workspace,
        context_id="hunk-side-collision-audit",
        material=True,
    )
    base_request = prepare_revision_job(workspace.root, "revise-from-audit").request
    base_finding = base_request.audit_findings[0]
    first = base_finding.model_copy(
        update={
            "finding_id": "AF000001",
            "draft_span": DraftSpan(start=0, end=1, text="a"),
            "requirement_ids": ["R000001"],
        }
    )
    second = base_finding.model_copy(
        update={
            "finding_id": "AF000002",
            "draft_span": DraftSpan(start=1, end=2, text="b"),
            "requirement_ids": ["R000002"],
        }
    )
    request = base_request.model_copy(
        update={
            "request_fingerprint": "f" * 64,
            "target_draft_markdown": "abab",
            "audit_findings": [first, second],
        }
    )
    claim = ClaimRecord(
        claim_id="C000001",
        draft_span=DraftSpan(start=0, end=5, text="aabba"),
        text="aabba",
        claim_type=ClaimType.VIDEO_FACT,
        evidence_ids=["E000001"],
        requirement_ids=[item.requirement_id for item in request.output_constraints.requirements],
        author_confidence=ClaimConfidence.HIGH,
    )
    collision = DraftSpan(start=3, end=4, text="b")
    response = RevisionResponseManifest(
        request_fingerprint=request.request_fingerprint,
        reviser=_identity(AgentRole.REVISER, "hunk-side-collision-reviser"),
        draft_markdown="aabba",
        claims=[claim],
        requirement_map=_requirement_map(request, claim.claim_id, "E000001"),
        resolutions=[
            ResolutionRecord(
                finding_id="AF000001",
                decision=ResolutionDecision.ACCEPT,
                resolution="The leading insertion is bound to its own anchor.",
                changed_spans=[DraftSpan(start=0, end=1, text="a")],
                evidence_ids=first.evidence_ids,
            ),
            ResolutionRecord(
                finding_id="AF000002",
                decision=ResolutionDecision.ACCEPT,
                resolution="The colliding changed and removed tuples retain their sides.",
                changed_spans=[collision],
                removed_spans=[collision],
                evidence_ids=second.evidence_ids,
            ),
        ],
        created_at=datetime.now(UTC),
    )
    assert SequenceMatcher(None, "abab", "aabba", autojunk=False).get_opcodes() == [
        ("insert", 0, 0, 0, 1),
        ("equal", 0, 2, 1, 3),
        ("insert", 2, 2, 3, 4),
        ("equal", 2, 3, 4, 5),
        ("delete", 3, 4, 5, 5),
    ]
    with pytest.raises(VepError) as collision_error:
        authoring_pipeline_module._validate_revision_response_semantics(
            response,
            request,
            forbidden_context_ids=(),
        )
    assert collision_error.value.code == "REVISION_RESPONSE_INVALID"
