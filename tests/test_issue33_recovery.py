from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_authoring_pipeline import (
    _audit_response,
    _author_response,
    _revision_response,
    _write_model,
)
from test_transcription_evidence import _response as _evidence_response
from test_transcription_evidence import _synthetic_captioned_source
from test_transcription_evidence import _write_response as _write_evidence_response
from typer.testing import CliRunner

import video_evidence_pipeline.application as application_module
import video_evidence_pipeline.storage.layout as layout_module
from video_evidence_pipeline.acquisition.box import BoxAdapter
from video_evidence_pipeline.acquisition.bundle import verify_acquired_bundle
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.application import (
    _import_audit_fixture,
    _import_authoring_fixture,
    _import_evidence_fixture,
    _import_revision_fixture,
    _import_transcript_refinement_fixture,
    acquire_job,
    approve_job,
    authorize_job,
    initialize_job,
    normalize_job,
    prepare_audit_job,
    prepare_authoring_job,
    prepare_evidence_job,
    prepare_revision_job,
    prepare_transcript_refinement_job,
    recover_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.domain.authoring import DraftSpan
from video_evidence_pipeline.domain.evidence import (
    AnalyzerExecutionMode,
    EvidenceConfidence,
    EvidenceRecord,
    InterpretationLevel,
)
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    PrimaryInputKind,
    SourceKind,
    SourceSpec,
    SupportingInputKind,
    SupportingInputSpec,
    job_fingerprint,
    job_to_yaml,
    load_job_yaml,
    requires_transcript_refinement,
)
from video_evidence_pipeline.domain.length import CharacterLengthConstraint
from video_evidence_pipeline.domain.refinement import (
    RefinementExecutionMode,
    RefinementResponseManifest,
    RefinerIdentity,
)
from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.jsonl import read_jsonl

_BOX_SHARED_LINK = "https://acme.ent.box.com/s/syntheticsharedtoken"
_FACT_ONE = "前年に100周年事業が行われた。"
_FACT_TWO = "5月31日に試合が行われた。"
_UNSUPPORTED_RELATION = "5月31日の100周年記念試合"
_BAD_SYNTHESIS_DRAFT = f"{_FACT_ONE} {_FACT_TWO} application evidence: {_UNSUPPORTED_RELATION}。"
_CLEAN_DRAFT = f"{_FACT_ONE} {_FACT_TWO} application evidence."


def _transcript_parent(
    tmp_path: Path,
    job_id: str,
    *,
    source_text: str | None = None,
    evidence_response_factory=None,
):
    repository = tmp_path / "r"
    repository.mkdir()
    shutil.copytree(Path(__file__).parents[1] / ".agents", repository / ".agents")
    source = repository / "source.txt"
    source.write_text("前年に100周年事業。\n\n5月31日に試合。\n", encoding="utf-8")
    if source_text is not None:
        source.write_text(source_text, encoding="utf-8")
    workspace, _job = initialize_job(
        repository,
        job_id=job_id,
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="ja",
        output_format="markdown",
        requirements=["根拠のある事実だけを記述する"],
        primary_input=PrimaryInputKind.TRANSCRIPT,
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic recovery fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)
    transcribe_job(workspace.root)
    refinement = prepare_transcript_refinement_job(workspace.root).request
    refinement_response = RefinementResponseManifest(
        request_fingerprint=refinement.request_fingerprint,
        refiner=RefinerIdentity(
            provider="fixture",
            model="fixture",
            version="1",
            executor="pytest",
            execution_mode=RefinementExecutionMode.TEST_FIXTURE,
            external_api_used=False,
        ),
        semantic_edits=[],
        ambiguities=[],
        created_at=datetime.now(UTC),
    )
    refinement_path = tmp_path / f"{job_id}-refinement.json"
    refinement_path.write_bytes(pretty_json_bytes(refinement_response))
    _import_transcript_refinement_fixture(workspace.root, refinement_path)
    evidence = prepare_evidence_job(workspace.root).request
    evidence_path = tmp_path / f"{job_id}-evidence.json"
    _write_evidence_response(
        evidence_path,
        (
            evidence_response_factory(evidence)
            if evidence_response_factory is not None
            else _evidence_response(evidence, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE)
        ),
    )
    _import_evidence_fixture(workspace.root, evidence_path)
    return workspace


def _prepare_fresh_semantic_inputs(tmp_path: Path, workspace, job_id: str) -> None:
    job = load_job_yaml(workspace.job_yaml)
    if requires_transcript_refinement(job):
        refinement = prepare_transcript_refinement_job(workspace.root).request
        refinement_response = RefinementResponseManifest(
            request_fingerprint=refinement.request_fingerprint,
            refiner=RefinerIdentity(
                provider="fixture",
                model="fixture",
                version="1",
                executor="pytest",
                execution_mode=RefinementExecutionMode.TEST_FIXTURE,
                external_api_used=False,
            ),
            semantic_edits=[],
            ambiguities=[],
            created_at=datetime.now(UTC),
        )
        refinement_path = tmp_path / f"{job_id}-fresh-refinement.json"
        refinement_path.write_bytes(pretty_json_bytes(refinement_response))
        _import_transcript_refinement_fixture(workspace.root, refinement_path)
        assert validate_job(workspace.root)["state"] == JobState.TRANSCRIPT_REFINED.value
    evidence = prepare_evidence_job(workspace.root).request
    evidence_path = tmp_path / f"{job_id}-fresh-evidence.json"
    _write_evidence_response(
        evidence_path,
        _evidence_response(evidence, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE),
    )
    _import_evidence_fixture(workspace.root, evidence_path)
    assert validate_job(workspace.root)["state"] == JobState.EVIDENCE_READY.value


def _human_review_ready_parent(tmp_path: Path, job_id: str):
    workspace = _transcript_parent(tmp_path, job_id)
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    author_path = tmp_path / f"{job_id}-author.json"
    _write_model(author_path, _author_response(request, context_id=f"{job_id}-author"))
    _import_authoring_fixture(workspace.root, author_path)
    audit_request = prepare_audit_job(workspace.root, "independent-content-audit").request
    audit_path = tmp_path / f"{job_id}-audit.json"
    _write_model(
        audit_path,
        _audit_response(
            audit_request,
            context_id=f"{job_id}-auditor",
            material=False,
        ),
    )
    audit = _import_audit_fixture(workspace.root, audit_path)
    assert audit.human_review is not None
    assert validate_job(workspace.root)["state"] == JobState.HUMAN_REVIEW_READY.value
    return workspace


def _author_response_with_draft(request, draft_markdown: str):
    response = _author_response(request)
    text = "application evidence"
    start = draft_markdown.index(text)
    claim = response.claims[0].model_copy(
        update={
            "draft_span": DraftSpan(start=start, end=start + len(text), text=text),
            "text": text,
        }
    )
    return response.model_copy(update={"draft_markdown": draft_markdown, "claims": [claim]})


def _unsupported_relation_evidence_response(request):
    transcript_text = " ".join(segment.normalized_text for segment in request.transcript_segments)
    assert _FACT_ONE in transcript_text
    assert _FACT_TWO in transcript_text
    assert _UNSUPPORTED_RELATION not in transcript_text
    return _evidence_response(
        request,
        execution_mode=AnalyzerExecutionMode.TEST_FIXTURE,
        evidence=[
            EvidenceRecord(
                evidence_id="E000001",
                observation=_UNSUPPORTED_RELATION,
                interpretation_level=InterpretationLevel.EXPLICIT_STATEMENT,
                confidence=EvidenceConfidence.HIGH,
                source_refs=list(request.transcript_refs),
                asserted_as_video_fact=True,
            )
        ],
    )


def _publish_content_integrity_audit(
    tmp_path: Path,
    workspace,
    *,
    context_id: str,
    category: str,
):
    request = prepare_audit_job(workspace.root, "independent-content-audit").request
    response = _audit_response(request, context_id=context_id, material=True)
    finding = response.findings[0].model_copy(update={"category": category})
    response_path = tmp_path / f"{context_id}.json"
    _write_model(response_path, response.model_copy(update={"findings": [finding]}))
    return _import_audit_fixture(workspace.root, response_path)


def _publish_initial_draft(
    tmp_path: Path,
    job_id: str,
    *,
    source_text: str | None = None,
    draft_markdown: str | None = None,
    evidence_response_factory=None,
):
    workspace = _transcript_parent(
        tmp_path,
        job_id,
        source_text=source_text,
        evidence_response_factory=evidence_response_factory,
    )
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    response_path = tmp_path / f"{job_id}-author.json"
    response = (
        _author_response_with_draft(request, draft_markdown)
        if draft_markdown is not None
        else _author_response(request)
    )
    _write_model(response_path, response)
    _import_authoring_fixture(workspace.root, response_path)
    return workspace


def _publish_revision(tmp_path: Path, workspace, *, context_id: str) -> None:
    request = prepare_revision_job(workspace.root, "revise-from-audit").request
    response_path = tmp_path / f"{context_id}.json"
    _write_model(response_path, _revision_response(request, context_id=context_id))
    _import_revision_fixture(workspace.root, response_path)


def _block_initial_draft(tmp_path: Path, workspace, *, job_id: str, category: str):
    _publish_content_integrity_audit(
        tmp_path,
        workspace,
        context_id=f"{job_id}-audit-1",
        category=category,
    )
    _publish_revision(tmp_path, workspace, context_id=f"{job_id}-revision-1")
    _publish_content_integrity_audit(
        tmp_path,
        workspace,
        context_id=f"{job_id}-audit-2",
        category=category,
    )
    _publish_revision(tmp_path, workspace, context_id=f"{job_id}-revision-2")
    blocked = _publish_content_integrity_audit(
        tmp_path,
        workspace,
        context_id=f"{job_id}-audit-3",
        category=category,
    )
    assert blocked.manifest.disposition.value == "blocked"
    assert validate_job(workspace.root)["state"] == JobState.BLOCKED.value
    return workspace, blocked


def _blocked_parent(
    tmp_path: Path,
    job_id: str,
    *,
    category: str,
    source_text: str | None = None,
    draft_markdown: str | None = None,
    evidence_response_factory=None,
):
    workspace = _publish_initial_draft(
        tmp_path,
        job_id,
        source_text=source_text,
        draft_markdown=draft_markdown,
        evidence_response_factory=evidence_response_factory,
    )
    return _block_initial_draft(tmp_path, workspace, job_id=job_id, category=category)


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _box_transcribed_parent(
    tmp_path: Path,
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = tmp_path / "r"
    repository.mkdir()
    shutil.copytree(Path(__file__).parents[1] / ".agents", repository / ".agents")
    source = _synthetic_captioned_source(tmp_path)
    payload = source.read_bytes()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.box.com" and request.url.path == "/2.0/shared_items":
            return httpx.Response(
                200,
                json={
                    "id": "67890",
                    "type": "file",
                    "name": source.name,
                    "size": len(payload),
                },
            )
        if request.url.host == "api.box.com" and request.url.path == "/2.0/files/67890":
            return httpx.Response(
                200,
                json={
                    "id": "67890",
                    "type": "file",
                    "name": source.name,
                    "size": len(payload),
                },
            )
        if request.url.host == "api.box.com" and request.url.path == "/2.0/files/67890/content":
            return httpx.Response(
                302,
                headers={"location": "https://dl.boxcloud.com/d/source.mkv?Signature=synthetic"},
            )
        assert request.url.host == "dl.boxcloud.com"
        assert request.headers.get("authorization") is None
        assert request.headers.get("boxapi") is None
        return httpx.Response(
            200,
            headers={"content-type": "video/x-matroska", "content-length": str(len(payload))},
            content=payload,
        )

    workspace, _job = initialize_job(
        repository,
        job_id=job_id,
        source_kind=SourceKind.BOX,
        source=_BOX_SHARED_LINK,
        task_type="report",
        language="en",
        output_format="markdown",
        requirements=["Do not infer causation."],
    )
    monkeypatch.setenv("VEP_BOX_ACCESS_TOKEN", "synthetic-token")
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic Box recovery fixture",
        allow_local_processing=True,
        allow_network_acquisition=True,
        allow_external_api=True,
        confirmer_identity="pytest",
    )
    runtime = AcquisitionRuntimeConfig(
        box_token_env="VEP_BOX_ACCESS_TOKEN",
        maximum_download_bytes=1024 * 1024,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        acquire_job(
            workspace.root,
            runtime,
            BoxAdapter(runtime, client=client, resolver=lambda _host, _port: ["93.184.216.34"]),
        )
    normalize_job(workspace.root)
    transcribe_job(workspace.root)
    return workspace, requests


def test_recovery_fork_reuses_only_pre_semantic_artifacts_and_keeps_parent_immutable(
    tmp_path: Path,
) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "p", category="content-integrity")
    parent_before = _file_bytes(parent.root)
    parent_transitions_before = parent.transitions_jsonl.read_bytes()

    runner = CliRunner()
    invoked = runner.invoke(
        app,
        [
            "job",
            "recover",
            str(parent.root),
            "c",
            "--confirm",
            "--basis",
            "restart semantic processing after bounded content-integrity review",
            "--json",
        ],
    )

    assert invoked.exit_code == 0, invoked.stdout
    payload = json.loads(invoked.stdout)
    assert payload["state"] == JobState.TRANSCRIBED.value
    assert payload["parent_job_id"] == parent.root.name
    assert payload["parent_finding_ids"] == []
    child_root = Path(payload["workspace"])
    assert validate_job(child_root)["state"] == JobState.TRANSCRIBED.value
    child_job = load_job_yaml(child_root / "job.yaml")
    assert child_job.primary_input is not None
    assert child_job.primary_input.kind is PrimaryInputKind.TRANSCRIPT
    assert _file_bytes(parent.root) == parent_before
    assert parent.transitions_jsonl.read_bytes() == parent_transitions_before

    child_transitions = read_jsonl(
        child_root / "manifests" / "transitions.jsonl",
        TransitionRecord,
    )
    assert [item.to_state for item in child_transitions] == [
        JobState.CREATED,
        JobState.AUTHORIZED,
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
    ]
    assert all(item.job_id == "c" for item in child_transitions)
    assert child_transitions[-1].trigger == "recovery.reuse-transcript"

    child = child_root
    assert not (child / "manifests" / "evidence-manifest.json").exists()
    assert not (child / "manifests" / "authoring" / "draft-v001.json").exists()
    assert not (child / "manifests" / "authoring" / "audit-v003.json").exists()
    assert not any(path.is_file() for path in (child / "refinement").rglob("*"))
    assert not any(path.is_file() for path in (child / "evidence").rglob("*"))
    assert not any(path.is_file() for path in (child / "authoring").rglob("*"))
    assert not any(path.is_file() for path in (child / "human-review").rglob("*"))
    assert not any(path.is_file() for path in (child / "approval").rglob("*"))
    assert not any(path.is_file() for path in (child / "exports").rglob("*"))

    recovery = json.loads((child / "manifests" / "recovery-manifest.json").read_text("utf-8"))
    assert recovery["parent_job_id"] == parent.root.name
    assert recovery["parent_final_draft_sha256"] is None
    assert recovery["parent_finding_ids"] == []


def test_recovery_accepts_a_blocked_parent_without_category_gate(tmp_path: Path) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "q", category="evidence-binding")

    recovered = recover_job(
        parent.root,
        "d",
        confirm=True,
        basis="recover from the pre-semantic boundary",
    )

    assert validate_job(recovered.workspace.root)["state"] == JobState.TRANSCRIBED.value


def test_recovery_forks_human_review_ready_parent_with_changed_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _human_review_ready_parent(tmp_path, "successful-parent")
    parent_before = _file_bytes(parent.root)
    parent_job = load_job_yaml(parent.job_yaml)
    child_job = parent_job.model_copy(
        update={
            "job_id": "successful-child",
            "created_at": datetime.now(UTC),
            "assignment": AssignmentSpec(
                task_type="short-report",
                language="en",
                requirements=["Use only the verified transcript."],
                length=CharacterLengthConstraint(max_chars=300),
            ),
        }
    )
    child_spec = tmp_path / "successful-child.yaml"
    child_spec.write_text(job_to_yaml(child_job), encoding="utf-8")

    def forbidden_stage(*_args, **_kwargs):
        pytest.fail("recovery must not execute acquisition, normalization, or transcription")

    monkeypatch.setattr(application_module, "acquire_job", forbidden_stage)
    monkeypatch.setattr(application_module, "normalize_job", forbidden_stage)
    monkeypatch.setattr(application_module, "transcribe_job", forbidden_stage)
    runner = CliRunner()
    invoked = runner.invoke(
        app,
        [
            "job",
            "recover",
            str(parent.root),
            child_job.job_id,
            "--confirm",
            "--basis",
            "rerun a successful transcript under a changed assignment",
            "--child-spec",
            str(child_spec),
            "--json",
        ],
    )

    assert invoked.exit_code == 0, invoked.stdout
    child = parent.jobs_root / child_job.job_id
    assert json.loads(invoked.stdout)["state"] == JobState.TRANSCRIBED.value
    assert validate_job(child, deep=True)["state"] == JobState.TRANSCRIBED.value
    stored_child = load_job_yaml(child / "job.yaml")
    assert stored_child.assignment == child_job.assignment
    assert stored_child.assignment.length is not None
    assert stored_child.assignment.length.max_chars == 300
    assert _file_bytes(parent.root) == parent_before
    transitions = read_jsonl(child / "manifests" / "transitions.jsonl", TransitionRecord)
    assert [record.to_state for record in transitions] == [
        JobState.CREATED,
        JobState.AUTHORIZED,
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
    ]
    for relative in (
        "refinement",
        "evidence",
        "authoring",
        "human-review",
        "approval",
        "exports",
    ):
        assert not any(path.is_file() for path in (child / relative).rglob("*"))

    approval_target = json.loads(parent.human_review_manifest_json.read_text("utf-8"))[
        "approval_target_sha256"
    ]
    approve_job(
        parent.root,
        approval_target_sha256=approval_target,
        reviewer="pytest-reviewer",
        basis="progress the parent after child creation",
        confirm=True,
    )
    assert validate_job(parent.root)["state"] == JobState.HUMAN_APPROVED.value
    assert validate_job(child, deep=True)["state"] == JobState.TRANSCRIBED.value


def test_recovery_rejects_a_pre_transcript_parent(tmp_path: Path) -> None:
    repository = tmp_path / "r"
    repository.mkdir()
    shutil.copytree(Path(__file__).parents[1] / ".agents", repository / ".agents")
    source = repository / "source.txt"
    source.write_text("verified input is not transcribed yet", encoding="utf-8")
    workspace, _job = initialize_job(
        repository,
        job_id="pre-transcript-parent",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="en",
        output_format="markdown",
        primary_input=PrimaryInputKind.TRANSCRIPT,
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized pre-transcript fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)

    with pytest.raises(VepError) as error:
        recover_job(
            workspace.root,
            "pre-transcript-child",
            confirm=True,
            basis="must reject before transcript verification",
        )
    assert error.value.code == "RECOVERY_PARENT_NOT_ELIGIBLE"
    assert not (workspace.jobs_root / "pre-transcript-child").exists()


def test_recovery_rejects_changed_source_and_supporting_input_identity(tmp_path: Path) -> None:
    parent = _human_review_ready_parent(tmp_path, "identity-parent")
    parent_job = load_job_yaml(parent.job_yaml)
    other_source = tmp_path / "other-source.txt"
    other_source.write_text("different source", encoding="utf-8")
    note = tmp_path / "note.txt"
    note.write_text("new supporting input", encoding="utf-8")
    children = (
        parent_job.model_copy(
            update={
                "job_id": "changed-source-child",
                "created_at": datetime.now(UTC),
                "source": SourceSpec(kind=SourceKind.LOCAL_FILE, locator=str(other_source)),
            }
        ),
        parent_job.model_copy(
            update={
                "job_id": "changed-support-child",
                "created_at": datetime.now(UTC),
                "supporting_inputs": [
                    SupportingInputSpec(kind=SupportingInputKind.NOTES, locator=str(note))
                ],
            }
        ),
    )
    for child_job in children:
        child_spec = tmp_path / f"{child_job.job_id}.yaml"
        child_spec.write_text(job_to_yaml(child_job), encoding="utf-8")
        with pytest.raises(VepError) as error:
            recover_job(
                parent.root,
                child_job.job_id,
                confirm=True,
                basis="identity changes are not assignment-only forks",
                child_spec_path=child_spec,
            )
        assert error.value.code == "RECOVERY_SOURCE_IDENTITY_MISMATCH"
        assert not (parent.jobs_root / child_job.job_id).exists()


def test_recovery_changed_assignment_reuses_only_source_artifacts_and_starts_fresh_authoring(
    tmp_path: Path,
) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "assignment-a", category="content-integrity")
    parent_job = load_job_yaml(parent.job_yaml)
    child_job = parent_job.model_copy(
        update={
            "job_id": "assignment-b",
            "created_at": datetime.now(UTC),
            "assignment": AssignmentSpec(
                task_type="short-report",
                language="en",
                requirements=["Bの要件だけを満たす"],
                length=CharacterLengthConstraint(min_chars=1, max_chars=300),
            ),
        }
    )
    child_spec = tmp_path / "assignment-b.yaml"
    child_spec.write_text(job_to_yaml(child_job), encoding="utf-8")

    recovered = recover_job(
        parent.root,
        "assignment-b",
        confirm=True,
        basis="reuse immutable source transcript under a changed assignment",
        child_spec_path=child_spec,
    )
    child = recovered.workspace
    stored_child = load_job_yaml(child.job_yaml)
    assert stored_child.assignment == child_job.assignment
    assert stored_child.assignment != parent_job.assignment
    assert validate_job(child.root)["state"] == JobState.TRANSCRIBED.value
    assert not any(path.is_file() for path in (child.root / "evidence").rglob("*"))
    assert not any(path.is_file() for path in (child.root / "authoring").rglob("*"))
    assert not any(path.is_file() for path in (child.root / "human-review").rglob("*"))
    assert not any(path.is_file() for path in (child.root / "approval").rglob("*"))
    assert not any(path.is_file() for path in (child.root / "exports").rglob("*"))

    _prepare_fresh_semantic_inputs(tmp_path, child, "assignment-b")
    request = prepare_authoring_job(child.root, "draft-video-report").request
    assert [item.text for item in request.output_constraints.requirements] == [
        "Bの要件だけを満たす"
    ]
    assert request.output_constraints.length == child_job.assignment.length
    assert request.output_constraints.effective_length is not None
    response_path = tmp_path / "assignment-b-author.json"
    _write_model(response_path, _author_response(request))
    imported = _import_authoring_fixture(child.root, response_path)
    assert imported.manifest.revision_count == 0
    assert imported.manifest.semantic_revision_count == 0
    assert imported.manifest.length_check is not None
    assert imported.manifest.length_check.passed


def test_recovery_copy_failure_removes_partial_child_and_allows_same_id_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "retryp", category="content-integrity")
    parent_before = _file_bytes(parent.root)
    original_ensure = layout_module.ensure_private_directory
    original_copy = application_module.atomic_copy_exclusive
    copy_count = 0

    def fail_child_directory(path, *args, **kwargs):
        if path.name == "authorization" and path.parent.name == "retryc":
            raise RuntimeError("synthetic recovery directory failure")
        return original_ensure(path, *args, **kwargs)

    monkeypatch.setattr(layout_module, "ensure_private_directory", fail_child_directory)
    with pytest.raises(RuntimeError, match="recovery directory failure"):
        recover_job(
            parent.root,
            "retryc",
            confirm=True,
            basis="retry the same child after a bounded directory failure",
        )
    assert not (parent.jobs_root / "retryc").exists()
    assert _file_bytes(parent.root) == parent_before
    monkeypatch.setattr(layout_module, "ensure_private_directory", original_ensure)

    def fail_second_copy(*args, **kwargs):
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
            raise RuntimeError("synthetic recovery copy failure")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(application_module, "atomic_copy_exclusive", fail_second_copy)
    with pytest.raises(RuntimeError, match="recovery copy failure"):
        recover_job(
            parent.root,
            "retryc",
            confirm=True,
            basis="retry the same child after a bounded copy failure",
        )
    assert not (parent.jobs_root / "retryc").exists()
    assert _file_bytes(parent.root) == parent_before

    monkeypatch.setattr(application_module, "atomic_copy_exclusive", original_copy)
    recovered = recover_job(
        parent.root,
        "retryc",
        confirm=True,
        basis="retry the same child after a bounded copy failure",
    )
    assert validate_job(recovered.workspace.root)["state"] == JobState.TRANSCRIBED.value
    assert _file_bytes(parent.root) == parent_before


def test_concurrent_recovery_same_child_id_fails_closed_and_preserves_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "racep", category="content-integrity")
    parent_before = _file_bytes(parent.root)
    copy_started = threading.Event()
    release_copy = threading.Event()
    original_copy = application_module._copy_recovery_artifacts

    def blocking_copy(*args, **kwargs) -> None:
        copy_started.set()
        if not release_copy.wait(timeout=5):
            raise RuntimeError("concurrent recovery test timed out")
        original_copy(*args, **kwargs)

    monkeypatch.setattr(application_module, "_copy_recovery_artifacts", blocking_copy)
    results = []
    failures: list[BaseException] = []

    def recover() -> None:
        try:
            results.append(
                recover_job(
                    parent.root,
                    "racec",
                    confirm=True,
                    basis="serialize recovery creation for the same child ID",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    first = threading.Thread(target=recover)
    second = threading.Thread(target=recover)
    first.start()
    assert copy_started.wait(timeout=5)
    second.start()
    second.join(timeout=5)
    release_copy.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], VepError)
    assert failures[0].code == "RECOVERY_CHILD_BUSY"
    assert validate_job(results[0].workspace.root)["state"] == JobState.TRANSCRIBED.value
    assert _file_bytes(parent.root) == parent_before


def test_recovery_and_job_init_atomically_claim_same_child_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "initracep", category="content-integrity")
    parent_before = _file_bytes(parent.root)
    child_id = "initracec"
    child_root = parent.jobs_root / child_id
    recovery_at_claim = threading.Event()
    release_recovery = threading.Event()
    path_type = type(child_root)
    original_mkdir = path_type.mkdir

    def synchronized_root_mkdir(path: Path, *args, **kwargs) -> None:
        if (
            path.name == child_id
            and path.parent.name == "jobs"
            and threading.current_thread().name == "recovery-claim"
        ):
            recovery_at_claim.set()
            if not release_recovery.wait(timeout=10):
                raise RuntimeError("recovery root claim test timed out")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "mkdir", synchronized_root_mkdir)
    source = tmp_path / "ordinary-source.txt"
    source.write_text("ordinary job source", encoding="utf-8")
    results: dict[str, object] = {}
    failures: list[BaseException] = []

    def recover() -> None:
        try:
            results["recovery"] = recover_job(
                parent.root,
                child_id,
                confirm=True,
                basis="atomically claim the recovery child workspace",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def initialize() -> None:
        try:
            results["ordinary"] = initialize_job(
                parent.jobs_root.parent.parent,
                job_id=child_id,
                source_kind=SourceKind.LOCAL_FILE,
                source=str(source),
                task_type="report",
                language="ja",
                output_format="markdown",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    recovery_thread = threading.Thread(target=recover, name="recovery-claim")
    ordinary_thread = threading.Thread(target=initialize)
    recovery_thread.start()
    assert recovery_at_claim.wait(timeout=10)
    ordinary_thread.start()
    ordinary_thread.join(timeout=10)
    release_recovery.set()
    recovery_thread.join(timeout=10)

    assert not recovery_thread.is_alive()
    assert not ordinary_thread.is_alive()
    assert results.keys() == {"ordinary"}
    assert len(failures) == 1
    assert isinstance(failures[0], VepError)
    assert failures[0].code == "JOB_EXISTS"
    assert child_root.exists()
    assert (child_root / "job.yaml").exists()
    assert validate_job(child_root)["state"] == JobState.CREATED.value
    assert _file_bytes(parent.root) == parent_before


def test_recovery_provenance_tamper_is_rejected_against_the_parent(tmp_path: Path) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "tamperp", category="content-integrity")
    recovered = recover_job(
        parent.root,
        "tamperc",
        confirm=True,
        basis="preserve the immutable parent provenance",
    )
    payload = json.loads(recovered.workspace.recovery_manifest_json.read_text("utf-8"))
    payload["parent_job_fingerprint"] = "0" * 64
    recovered.workspace.recovery_manifest_json.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(VepError) as captured:
        validate_job(recovered.workspace.root)
    assert captured.value.code == "RECOVERY_PROVENANCE_MISMATCH"


def test_recovery_child_detects_parent_presemantic_manifest_tamper(tmp_path: Path) -> None:
    parent = _human_review_ready_parent(tmp_path, "parent-presemantic-tamper")
    recovered = recover_job(
        parent.root,
        "parent-presemantic-tamper-child",
        confirm=True,
        basis="bind the child to verified parent pre-semantic artifacts",
    )
    payload = json.loads(parent.transcript_manifest_json.read_text("utf-8"))
    payload["job_id"] = "different-parent"
    parent.transcript_manifest_json.write_bytes(pretty_json_bytes(payload))

    with pytest.raises(VepError) as captured:
        validate_job(recovered.workspace.root)
    assert captured.value.code in {
        "TRANSCRIPT_MANIFEST_MISMATCH",
        "TRANSCRIPT_BUNDLE_INVALID",
    }


def test_recovery_child_deep_validation_detects_parent_artifact_byte_tamper(
    tmp_path: Path,
) -> None:
    parent = _human_review_ready_parent(tmp_path, "parent-artifact-tamper")
    recovered = recover_job(
        parent.root,
        "parent-artifact-tamper-child",
        confirm=True,
        basis="bind the child to immutable parent artifact bytes",
    )
    transcript = json.loads(parent.transcript_manifest_json.read_text("utf-8"))
    segments_path = parent.safe_path(transcript["segments_artifact"]["relative_storage_path"])
    before = segments_path.stat()
    payload = segments_path.read_bytes()
    replacement = b"x" if payload[:1] != b"x" else b"y"
    segments_path.write_bytes(replacement + payload[1:])
    os.utime(segments_path, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(VepError) as captured:
        validate_job(recovered.workspace.root, deep=True)
    assert captured.value.code == "TRANSCRIPT_BUNDLE_TAMPERED"


def test_recovery_provenance_manifest_deletion_is_rejected(tmp_path: Path) -> None:
    parent, _blocked = _blocked_parent(tmp_path, "missingp", category="content-integrity")
    recovered = recover_job(
        parent.root,
        "missingc",
        confirm=True,
        basis="recovery transitions require immutable provenance",
    )
    recovered.workspace.recovery_manifest_json.unlink()

    with pytest.raises(VepError) as captured:
        validate_job(recovered.workspace.root)
    assert captured.value.code == "RECOVERY_PROVENANCE_MISMATCH"


@pytest.mark.parametrize(
    ("permission", "slug"),
    [
        ("allow_local_processing", "local"),
        ("allow_network_acquisition", "network"),
        ("allow_external_api", "external"),
    ],
)
def test_recovery_authorization_permission_tamper_is_rejected_against_the_parent(
    tmp_path: Path,
    permission: str,
    slug: str,
) -> None:
    parent, _blocked = _blocked_parent(tmp_path, f"auth-{slug}-p", category="content-integrity")
    recovered = recover_job(
        parent.root,
        f"auth-{slug}-c",
        confirm=True,
        basis="preserve parent authorization permissions",
    )
    payload = json.loads(recovered.workspace.authorization_json.read_text("utf-8"))
    payload[permission] = not payload[permission]
    recovered.workspace.authorization_json.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(VepError) as captured:
        validate_job(recovered.workspace.root)
    assert captured.value.code == "RECOVERY_PROVENANCE_MISMATCH"


def test_recovery_rebinds_a_box_source_without_reacquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, requests = _box_transcribed_parent(tmp_path, "boxp", monkeypatch)
    assert load_job_yaml(parent.job_yaml).primary_input is None
    _prepare_fresh_semantic_inputs(tmp_path, parent, "boxp")
    request = prepare_authoring_job(parent.root, "draft-video-report").request
    response_path = tmp_path / "boxp-author.json"
    _write_model(response_path, _author_response(request))
    _import_authoring_fixture(parent.root, response_path)
    parent, _blocked = _block_initial_draft(
        tmp_path,
        parent,
        job_id="boxp",
        category="content-integrity",
    )
    parent_before = _file_bytes(parent.root)
    acquisition_calls_before = len(requests)

    recovered = recover_job(
        parent.root,
        "boxc",
        confirm=True,
        basis="restart semantic processing without another Box retrieval",
    )

    assert len(requests) == acquisition_calls_before == 4
    child_job = load_job_yaml(recovered.workspace.job_yaml)
    assert child_job.primary_input is not None
    assert child_job.primary_input.kind is PrimaryInputKind.VIDEO
    assert requires_transcript_refinement(child_job) is True
    assert (
        verify_acquired_bundle(recovered.workspace, child_job).content_sha256
        == verify_acquired_bundle(parent, load_job_yaml(parent.job_yaml)).content_sha256
    )
    child_source = json.loads(recovered.workspace.source_manifest_json.read_text("utf-8"))
    assert child_source["job_id"] == "boxc"
    assert child_source["source_kind"] == SourceKind.BOX.value
    assert child_source["acquisition_evidence"]["adapter_kind"] == SourceKind.BOX.value
    assert child_source["artifact_record"]["configuration_sha256"] == job_fingerprint(child_job)
    child_authorization = json.loads(recovered.workspace.authorization_json.read_text("utf-8"))
    assert child_authorization["job_id"] == "boxc"
    assert child_authorization["allow_local_processing"] is True
    assert child_authorization["allow_network_acquisition"] is True
    assert child_authorization["allow_external_api"] is True
    assert validate_job(recovered.workspace.root)["state"] == JobState.TRANSCRIBED.value

    _prepare_fresh_semantic_inputs(tmp_path, recovered.workspace, "boxc")
    authoring = prepare_authoring_job(recovered.workspace.root, "draft-video-report").request
    authoring_path = tmp_path / "boxc-author.json"
    _write_model(authoring_path, _author_response(authoring))
    initial = _import_authoring_fixture(recovered.workspace.root, authoring_path)

    assert initial.manifest.draft_version == "v001"
    assert initial.manifest.revision_count == 0
    assert json.loads(recovered.workspace.refinement_manifest_json.read_text("utf-8"))[
        "job_id"
    ] == ("boxc")
    assert json.loads(recovered.workspace.evidence_manifest_json.read_text("utf-8"))["job_id"] == (
        "boxc"
    )
    assert _file_bytes(parent.root) == parent_before


def test_recovery_forks_successful_box_parent_without_network_normalize_or_asr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, requests = _box_transcribed_parent(tmp_path, "successful-box-parent", monkeypatch)
    _prepare_fresh_semantic_inputs(tmp_path, parent, "successful-box-parent")
    author_request = prepare_authoring_job(parent.root, "draft-video-report").request
    author_path = tmp_path / "successful-box-parent-author.json"
    _write_model(author_path, _author_response(author_request))
    _import_authoring_fixture(parent.root, author_path)
    audit_request = prepare_audit_job(parent.root, "independent-content-audit").request
    audit_path = tmp_path / "successful-box-parent-audit.json"
    _write_model(audit_path, _audit_response(audit_request, material=False))
    _import_audit_fixture(parent.root, audit_path)
    assert validate_job(parent.root)["state"] == JobState.HUMAN_REVIEW_READY.value
    parent_before = _file_bytes(parent.root)
    network_calls_before = len(requests)

    def forbidden_stage(*_args, **_kwargs):
        pytest.fail("successful-parent recovery must only copy verified artifacts")

    monkeypatch.setattr(application_module, "acquire_job", forbidden_stage)
    monkeypatch.setattr(application_module, "normalize_job", forbidden_stage)
    monkeypatch.setattr(application_module, "transcribe_job", forbidden_stage)
    recovered = recover_job(
        parent.root,
        "successful-box-child",
        confirm=True,
        basis="reuse a successful Box transcript without rerunning pre-semantic stages",
    )

    assert len(requests) == network_calls_before
    assert validate_job(recovered.workspace.root, deep=True)["state"] == (
        JobState.TRANSCRIBED.value
    )
    assert _file_bytes(parent.root) == parent_before


def test_recovery_restarts_a_bad_synthesis_at_fresh_v001_and_reaches_clean_audit(
    tmp_path: Path,
) -> None:
    parent, _blocked = _blocked_parent(
        tmp_path,
        "s",
        category="content-integrity",
        source_text=f"{_FACT_ONE}\n{_FACT_TWO}\n",
        draft_markdown=_BAD_SYNTHESIS_DRAFT,
        evidence_response_factory=_unsupported_relation_evidence_response,
    )
    parent_transcript = json.loads(parent.transcript_manifest_json.read_text("utf-8"))
    parent_segments = parent.safe_path(
        parent_transcript["segments_artifact"]["relative_storage_path"]
    ).read_text("utf-8")
    assert _FACT_ONE in parent_segments
    assert _FACT_TWO in parent_segments
    assert _UNSUPPORTED_RELATION not in parent_segments
    parent_evidence_manifest = json.loads(parent.evidence_manifest_json.read_text("utf-8"))
    assert _UNSUPPORTED_RELATION in parent.safe_path(
        parent_evidence_manifest["evidence_artifact"]["relative_storage_path"]
    ).read_text("utf-8")
    assert _UNSUPPORTED_RELATION in parent.safe_path("authoring/drafts/v003/draft.md").read_text(
        "utf-8"
    )

    recovered = recover_job(
        parent.root,
        "t",
        confirm=True,
        basis="rebuild the unsupported relation from separated transcript facts",
    )
    child = recovered.workspace
    _prepare_fresh_semantic_inputs(tmp_path, child, "t")
    authoring = prepare_authoring_job(child.root, "draft-video-report").request
    authoring_path = tmp_path / "t-author.json"
    _write_model(authoring_path, _author_response_with_draft(authoring, _CLEAN_DRAFT))
    initial = _import_authoring_fixture(child.root, authoring_path)
    clean_audit_request = prepare_audit_job(child.root, "independent-content-audit").request
    clean_audit_path = tmp_path / "t-clean-audit.json"
    _write_model(
        clean_audit_path,
        _audit_response(clean_audit_request, context_id="t-clean", material=False),
    )
    clean = _import_audit_fixture(child.root, clean_audit_path)

    assert initial.manifest.draft_version == "v001"
    assert initial.manifest.revision_count == 0
    child_evidence_manifest = json.loads(child.evidence_manifest_json.read_text("utf-8"))
    assert _UNSUPPORTED_RELATION not in child.safe_path(
        child_evidence_manifest["evidence_artifact"]["relative_storage_path"]
    ).read_text("utf-8")
    assert _UNSUPPORTED_RELATION not in child.safe_path(
        initial.manifest.draft_artifact.relative_storage_path
    ).read_text("utf-8")
    assert clean.manifest.p0_count == 0
    assert clean.manifest.p1_count == 0
    assert clean.manifest.disposition.value == "human_review_ready"
    assert validate_job(child.root)["state"] == JobState.HUMAN_REVIEW_READY.value
