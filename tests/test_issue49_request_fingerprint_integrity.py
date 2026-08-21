from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_authoring_pipeline import (
    _audit_response,
    _author_response,
    _evidence_ready_job,
    _revision_response,
    _write_model,
)
from test_transcription_evidence import (
    _application_transcribed_job,
)
from test_transcription_evidence import (
    _response as _evidence_response,
)
from test_transcription_evidence import (
    _write_response as _write_evidence_response,
)

import video_evidence_pipeline.application as application
from video_evidence_pipeline.application import (
    _import_audit_fixture,
    _import_authoring_fixture,
    _import_evidence_fixture,
    _import_revision_fixture,
    _import_transcript_refinement_fixture,
    acquire_job,
    authorize_job,
    import_audit_job,
    import_authoring_job,
    import_evidence_job,
    import_revision_job,
    import_transcript_refinement_job,
    initialize_job,
    job_guide,
    normalize_job,
    prepare_audit_job,
    prepare_authoring_job,
    prepare_evidence_job,
    prepare_revision_job,
    prepare_transcript_refinement_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.domain.authoring import AgentExecutionMode
from video_evidence_pipeline.domain.evidence import AnalyzerExecutionMode
from video_evidence_pipeline.domain.job import PrimaryInputKind, SourceKind
from video_evidence_pipeline.domain.refinement import (
    RefinementExecutionMode,
    RefinementResponseManifest,
    RefinerIdentity,
)
from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.layout import JobWorkspace

_TAMPERED_FINGERPRINT = "f" * 64


@dataclass(frozen=True)
class _PendingStage:
    workspace: JobWorkspace
    request_path: Path
    response: object
    prepare: Callable[[], object]
    import_response: Callable[[Path], object]
    publication_paths: tuple[Path, ...]


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _tamper_request_fingerprint(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["request_fingerprint"] = _TAMPERED_FINGERPRINT
    path.write_bytes(pretty_json_bytes(payload))


def _refinement_stage(tmp_path: Path) -> _PendingStage:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "source.txt"
    source.write_text("first proposition\n\nsecond proposition\n", encoding="utf-8")
    workspace, _job = initialize_job(
        repository,
        job_id="request-fingerprint-refinement",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="en",
        output_format="markdown",
        primary_input=PrimaryInputKind.TRANSCRIPT,
    )
    shutil.copytree(Path(__file__).parents[1] / ".agents", repository / ".agents")
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic request-integrity fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)
    transcribe_job(workspace.root)
    request = prepare_transcript_refinement_job(workspace.root).request
    response = RefinementResponseManifest(
        request_fingerprint=request.request_fingerprint,
        refiner=RefinerIdentity(
            provider="fixture",
            model="fixture",
            version="1",
            executor="pytest",
            execution_mode=RefinementExecutionMode.TEST_FIXTURE,
            external_api_used=False,
        ),
        created_at=datetime.now(UTC),
    )
    return _PendingStage(
        workspace=workspace,
        request_path=workspace.refinement_request_json,
        response=response,
        prepare=lambda: prepare_transcript_refinement_job(workspace.root),
        import_response=lambda path: import_transcript_refinement_job(workspace.root, path),
        publication_paths=(workspace.refinement_manifest_json,),
    )


def _evidence_stage(tmp_path: Path) -> _PendingStage:
    workspace, _transcription = _application_transcribed_job(
        tmp_path, "request-fingerprint-evidence"
    )
    request = prepare_evidence_job(workspace.root).request
    response = _evidence_response(request, execution_mode=AnalyzerExecutionMode.EXTERNAL_EXCHANGE)
    return _PendingStage(
        workspace=workspace,
        request_path=workspace.evidence_request_json,
        response=response,
        prepare=lambda: prepare_evidence_job(workspace.root),
        import_response=lambda path: import_evidence_job(workspace.root, path),
        publication_paths=(workspace.evidence_manifest_json,),
    )


def _completed_author(workspace: JobWorkspace, tmp_path: Path) -> None:
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    response_path = tmp_path / "author-response.json"
    _write_model(response_path, _author_response(request))
    _import_authoring_fixture(workspace.root, response_path)


def _completed_material_audit(workspace: JobWorkspace, tmp_path: Path) -> None:
    request = prepare_audit_job(workspace.root, "independent-content-audit").request
    response_path = tmp_path / "audit-response.json"
    _write_model(response_path, _audit_response(request, material=True))
    _import_audit_fixture(workspace.root, response_path)


def _authoring_stage(tmp_path: Path, stage: str) -> _PendingStage:
    workspace = _evidence_ready_job(tmp_path, f"request-fingerprint-{stage}")
    if stage == "author":
        request = prepare_authoring_job(workspace.root, "draft-video-report").request
        return _PendingStage(
            workspace=workspace,
            request_path=workspace.safe_path("authoring/requests/author-v001.json"),
            response=_author_response(request, execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE),
            prepare=lambda: prepare_authoring_job(workspace.root, "draft-video-report"),
            import_response=lambda path: import_authoring_job(workspace.root, path),
            publication_paths=(workspace.safe_path("manifests/authoring/draft-v001.json"),),
        )
    _completed_author(workspace, tmp_path)
    if stage == "audit":
        request = prepare_audit_job(workspace.root, "independent-content-audit").request
        return _PendingStage(
            workspace=workspace,
            request_path=workspace.safe_path("authoring/requests/audit-v001.json"),
            response=_audit_response(
                request,
                execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
            ),
            prepare=lambda: prepare_audit_job(workspace.root, "independent-content-audit"),
            import_response=lambda path: import_audit_job(workspace.root, path),
            publication_paths=(workspace.safe_path("manifests/authoring/audit-v001.json"),),
        )
    _completed_material_audit(workspace, tmp_path)
    request = prepare_revision_job(workspace.root, "revise-from-audit").request
    return _PendingStage(
        workspace=workspace,
        request_path=workspace.safe_path("authoring/requests/revision-v002.json"),
        response=_revision_response(
            request,
            context_id="request-integrity-reviser",
            execution_mode=AgentExecutionMode.EXTERNAL_EXCHANGE,
        ),
        prepare=lambda: prepare_revision_job(workspace.root, "revise-from-audit"),
        import_response=lambda path: import_revision_job(workspace.root, path),
        publication_paths=(
            workspace.safe_path("manifests/authoring/revision-v002.json"),
            workspace.safe_path("manifests/authoring/draft-v002.json"),
        ),
    )


def _pending_stage(tmp_path: Path, stage: str) -> _PendingStage:
    stage_root = tmp_path / stage
    stage_root.mkdir()
    if stage == "refinement":
        return _refinement_stage(stage_root)
    if stage == "evidence":
        return _evidence_stage(stage_root)
    return _authoring_stage(stage_root, stage)


@pytest.mark.parametrize("stage", ["refinement", "evidence", "author", "audit", "revision"])
def test_pending_request_self_fingerprint_tamper_is_fail_closed_everywhere(
    tmp_path: Path,
    stage: str,
) -> None:
    pending = _pending_stage(tmp_path, stage)
    transitions_before = pending.workspace.transitions_jsonl.read_bytes()
    _tamper_request_fingerprint(pending.request_path)
    tampered_request = pending.request_path.read_bytes()
    tampered_mtime = pending.request_path.stat().st_mtime_ns

    guide_before = _tree_snapshot(pending.workspace.root)
    guide = job_guide(pending.workspace.root)
    assert guide["status"] == "integrity-error"
    assert guide["next_operation"] is None
    assert guide["command"] is None
    assert guide["migration_required"] is False
    assert "skill_path" not in guide
    assert _tree_snapshot(pending.workspace.root) == guide_before

    with pytest.raises(VepError, match="request fingerprint is inconsistent"):
        pending.prepare()
    assert pending.request_path.read_bytes() == tampered_request
    assert pending.request_path.stat().st_mtime_ns == tampered_mtime

    response = pending.response.model_copy(update={"request_fingerprint": _TAMPERED_FINGERPRINT})
    response_path = tmp_path / f"{stage}-tampered-response.json"
    _write_model(response_path, response)
    with pytest.raises(VepError, match="request fingerprint is inconsistent"):
        pending.import_response(response_path)

    assert pending.workspace.transitions_jsonl.read_bytes() == transitions_before
    assert all(not path.exists() for path in pending.publication_paths)
    assert pending.request_path.read_bytes() == tampered_request


@pytest.mark.parametrize("stage", ["refinement", "evidence", "author", "audit", "revision"])
def test_completed_request_fingerprint_tamper_fails_deep_validation(
    tmp_path: Path,
    stage: str,
) -> None:
    pending = _pending_stage(tmp_path, stage)
    response_path = tmp_path / f"{stage}-completed-response.json"
    if stage == "refinement":
        _write_model(response_path, pending.response)
        _import_transcript_refinement_fixture(pending.workspace.root, response_path)
    elif stage == "evidence":
        _write_evidence_response(response_path, pending.response)
        _import_evidence_fixture(pending.workspace.root, response_path)
    elif stage == "author":
        _write_model(response_path, pending.response)
        _import_authoring_fixture(pending.workspace.root, response_path)
    elif stage == "audit":
        _write_model(response_path, pending.response)
        _import_audit_fixture(pending.workspace.root, response_path)
    else:
        _write_model(response_path, pending.response)
        _import_revision_fixture(pending.workspace.root, response_path)

    validate_job(pending.workspace.root, deep=True)
    _tamper_request_fingerprint(pending.request_path)
    with pytest.raises(VepError, match="request fingerprint is inconsistent"):
        validate_job(pending.workspace.root, deep=True)


@pytest.mark.parametrize("stage", ["refinement", "evidence", "author", "audit", "revision"])
def test_request_fingerprint_tamper_during_import_cannot_cross_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    pending = _pending_stage(tmp_path, stage)
    transitions_before = pending.workspace.transitions_jsonl.read_bytes()
    response_path = tmp_path / f"{stage}-race-response.json"
    _write_model(response_path, pending.response)
    reader_name = {
        "refinement": "read_refinement_response",
        "evidence": "read_evidence_response",
        "author": "read_authoring_response",
        "audit": "read_audit_response",
        "revision": "read_revision_response",
    }[stage]
    original_reader = getattr(application, reader_name)

    def tampering_reader(*args, **kwargs):
        response = original_reader(*args, **kwargs)
        _tamper_request_fingerprint(pending.request_path)
        return response

    monkeypatch.setattr(application, reader_name, tampering_reader)
    importer = {
        "refinement": _import_transcript_refinement_fixture,
        "evidence": _import_evidence_fixture,
        "author": _import_authoring_fixture,
        "audit": _import_audit_fixture,
        "revision": _import_revision_fixture,
    }[stage]
    with pytest.raises(VepError, match="request fingerprint is inconsistent"):
        importer(pending.workspace.root, response_path)

    assert pending.workspace.transitions_jsonl.read_bytes() == transitions_before
    assert all(not path.exists() for path in pending.publication_paths)
