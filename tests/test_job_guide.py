from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import video_evidence_pipeline.application as application
import video_evidence_pipeline.cli as cli
from video_evidence_pipeline.application import (
    _guide_operation,
    default_authoring_skill,
    initialize_job,
    job_guide,
)
from video_evidence_pipeline.domain.job import PrimaryInputKind, SourceKind
from video_evidence_pipeline.domain.semantic import (
    PendingSemanticRequestAssessment,
    PendingSemanticRequestStatus,
)
from video_evidence_pipeline.domain.state import JobState


def _created_workspace(
    tmp_path: Path,
    *,
    task_type: str = "report",
    transcript: bool = False,
    source_kind: SourceKind = SourceKind.LOCAL_FILE,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = (
        tmp_path / ("source.txt" if transcript else "source.mp4")
        if source_kind is SourceKind.LOCAL_FILE
        else "https://example.invalid/source.mp4"
    )
    if isinstance(source, Path):
        source.write_text("synthetic", encoding="utf-8")
    workspace, job = initialize_job(
        tmp_path,
        job_id="guide-job",
        source_kind=source_kind,
        source=str(source),
        task_type=task_type,
        language="en",
        output_format="markdown",
        primary_input=PrimaryInputKind.TRANSCRIPT if transcript else PrimaryInputKind.VIDEO,
    )
    return workspace, job


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (JobState.CREATED, "authorize"),
        (JobState.AUTHORIZED, "acquire"),
        (JobState.ACQUIRED, "normalize"),
        (JobState.NORMALIZED, "transcribe"),
        (JobState.TRANSCRIBED, "refine_prepare"),
        (JobState.TRANSCRIPT_REFINED, "evidence_prepare"),
        (JobState.EVIDENCE_READY, "author_prepare"),
        (JobState.DRAFTED, "audit_prepare"),
        (JobState.REVISION_REQUIRED, "revision_prepare"),
        (JobState.HUMAN_REVIEW_READY, "human_approve"),
        (JobState.HUMAN_APPROVED, "export"),
    ],
)
def test_guide_maps_major_states_to_public_operations(
    tmp_path: Path,
    state: JobState,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, job = _created_workspace(tmp_path)
    status = {"authoring_integrity": {"draft_version": "v003"}}
    monkeypatch.setattr(
        application,
        "load_skill_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(lifecycle="active"),
    )

    result = _guide_operation(workspace, job, [], state, status)

    assert result["next_operation"] == expected
    assert set(result["side_effects"]) == {
        "network_acquisition",
        "normalization",
        "ffmpeg",
        "asr",
        "model_provision",
        "external_api_possible",
        "workspace_write",
    }
    if expected.endswith("_prepare"):
        assert Path(result["request_path"]).is_absolute()
        assert Path(result["skill_path"]).is_absolute()
        assert Path(result["response_schema_path"]).is_absolute()


def test_guide_reports_complete_and_terminal_states_without_a_command(tmp_path: Path) -> None:
    workspace, job = _created_workspace(tmp_path)
    for state, expected in (
        (JobState.EXPORTED, "complete"),
        (JobState.FAILED, "terminal"),
        (JobState.CANCELLED, "terminal"),
    ):
        result = _guide_operation(workspace, job, [], state, {})
        assert result["status"] == expected
        assert result["command"] is None
        assert result["side_effects"]["workspace_write"] is False


def test_guide_only_offers_recovery_from_a_transcript_bearing_block(tmp_path: Path) -> None:
    workspace, job = _created_workspace(tmp_path)
    eligible = [SimpleNamespace(to_state=JobState.TRANSCRIBED)]

    recover = _guide_operation(workspace, job, eligible, JobState.BLOCKED, {})
    refused = _guide_operation(workspace, job, [], JobState.BLOCKED, {})

    assert recover["next_operation"] == "recover"
    assert recover["side_effects"]["asr"] is False
    assert refused["next_operation"] is None
    assert "no safe public recovery" in refused["reason"]


def test_guide_refuses_fuzzy_author_skill_selection(tmp_path: Path) -> None:
    workspace, job = _created_workspace(tmp_path, task_type="custom research brief")

    result = _guide_operation(workspace, job, [], JobState.EVIDENCE_READY, {})

    assert result["status"] == "blocked"
    assert result["next_operation"] == "author_prepare"
    assert result["command"].endswith("--skill <skill-id>")


def test_network_source_guide_requires_an_explicit_one_time_authorization_choice(
    tmp_path: Path,
) -> None:
    workspace, job = _created_workspace(tmp_path, source_kind=SourceKind.DIRECT_HTTP)

    result = _guide_operation(workspace, job, [], JobState.CREATED, {})

    assert result["status"] == "human-action-required"
    assert result["next_operation"] == "authorize"
    assert result["command"] is None
    assert "one-time human decision" in result["reason"]
    choices = result["authorization_choices"]
    assert set(choices) == {
        "allow_network_local_semantics",
        "allow_network_external_semantics",
        "deny_network_local_semantics",
        "deny_network_external_semantics",
    }
    assert choices["allow_network_local_semantics"].endswith(
        "--allow-network-acquisition --deny-external-api"
    )
    assert choices["allow_network_external_semantics"].endswith(
        "--allow-network-acquisition --allow-external-api"
    )
    assert choices["deny_network_local_semantics"].endswith(
        "--deny-network-acquisition --deny-external-api"
    )
    assert choices["deny_network_external_semantics"].endswith(
        "--deny-network-acquisition --allow-external-api"
    )


def test_local_source_guide_requires_explicit_external_api_authorization_choice(
    tmp_path: Path,
) -> None:
    workspace, job = _created_workspace(tmp_path)

    result = _guide_operation(workspace, job, [], JobState.CREATED, {})

    assert result["status"] == "human-action-required"
    assert result["next_operation"] == "authorize"
    assert result["command"] is None
    assert "one-time human decision" in result["reason"]
    choices = result["authorization_choices"]
    assert choices["local_semantic_runner"].endswith("--deny-external-api")
    assert choices["external_semantic_runner"].endswith("--allow-external-api")


def test_network_acquisition_denial_blocks_acquire_but_local_source_stays_ready(
    tmp_path: Path,
) -> None:
    network_workspace, network_job = _created_workspace(
        tmp_path / "network",
        source_kind=SourceKind.DIRECT_HTTP,
    )
    local_workspace, local_job = _created_workspace(tmp_path / "local")
    denied = {"authorization": {"allow_network_acquisition": False}}
    allowed = {"authorization": {"allow_network_acquisition": True}}

    network = _guide_operation(
        network_workspace,
        network_job,
        [],
        JobState.AUTHORIZED,
        denied,
    )
    permitted_network = _guide_operation(
        network_workspace,
        network_job,
        [],
        JobState.AUTHORIZED,
        allowed,
    )
    local = _guide_operation(local_workspace, local_job, [], JobState.AUTHORIZED, denied)

    assert network["status"] == "blocked"
    assert network["next_operation"] is None
    assert network["command"] is None
    assert network["reason"].startswith("NETWORK_ACQUISITION_NOT_AUTHORIZED")
    assert permitted_network["status"] == "ready"
    assert permitted_network["next_operation"] == "acquire"
    assert permitted_network["side_effects"]["network_acquisition"] is True
    assert local["status"] == "ready"
    assert local["next_operation"] == "acquire"
    assert local["side_effects"]["network_acquisition"] is False


def test_author_skill_defaults_are_exact_and_deterministic() -> None:
    assert default_authoring_skill("report") == "draft-video-report"
    assert default_authoring_skill("summary") == "draft-video-summary"
    assert default_authoring_skill("meeting-minutes") == "draft-video-summary"
    assert default_authoring_skill("summary please") is None
    assert default_authoring_skill("Report") is None


def test_supplied_transcript_guidance_declares_no_ffmpeg_or_asr(tmp_path: Path) -> None:
    workspace, job = _created_workspace(tmp_path, transcript=True)

    normalization = _guide_operation(workspace, job, [], JobState.ACQUIRED, {})
    transcription = _guide_operation(workspace, job, [], JobState.NORMALIZED, {})

    assert normalization["side_effects"]["ffmpeg"] is False
    assert transcription["side_effects"]["asr"] is False
    assert transcription["side_effects"]["model_provision"] is False


def test_video_transcription_guidance_does_not_claim_model_provision(tmp_path: Path) -> None:
    workspace, job = _created_workspace(tmp_path)

    transcription = _guide_operation(workspace, job, [], JobState.NORMALIZED, {})

    assert transcription["side_effects"]["asr"] is True
    assert transcription["side_effects"]["model_provision"] is False


def test_guide_blocks_mapped_draft_skill_without_a_prepare_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, job = _created_workspace(tmp_path)

    def draft_skill(
        actual_workspace: object,
        skill_id: str,
        *,
        allowed: frozenset[str],
    ) -> SimpleNamespace:
        assert actual_workspace is workspace
        assert skill_id == "draft-video-report"
        assert allowed == frozenset({"draft-video-report", "draft-video-summary"})
        return SimpleNamespace(lifecycle="draft")

    monkeypatch.setattr(application, "load_skill_snapshot", draft_skill)

    result = _guide_operation(workspace, job, [], JobState.EVIDENCE_READY, {})

    assert result["status"] == "blocked"
    assert result["next_operation"] is None
    assert result["command"] is None
    assert result["reason"].startswith("DRAFT_SKILL_FORBIDDEN")
    assert result["side_effects"]["workspace_write"] is False


def test_guide_reports_stale_mapped_skill_without_a_prepare_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, job = _created_workspace(tmp_path)

    def stale_skill(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise application.integrity_error("SKILL_EVAL_RESULT_INVALID", "synthetic stale Skill")

    monkeypatch.setattr(application, "load_skill_snapshot", stale_skill)

    result = _guide_operation(workspace, job, [], JobState.EVIDENCE_READY, {})

    assert result["status"] == "integrity-error"
    assert result["next_operation"] is None
    assert result["command"] is None
    assert result["reason"].startswith("SKILL_SNAPSHOT_STALE")
    assert result["side_effects"]["workspace_write"] is False


def test_semantic_prepare_side_effects_exclude_later_external_api_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, job = _created_workspace(tmp_path)
    monkeypatch.setattr(
        application,
        "load_skill_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(lifecycle="active"),
    )

    denied = _guide_operation(
        workspace,
        job,
        [],
        JobState.EVIDENCE_READY,
        {"authorization": {"allow_external_api": False}},
    )
    authorized = _guide_operation(
        workspace,
        job,
        [],
        JobState.EVIDENCE_READY,
        {"authorization": {"allow_external_api": True}},
    )

    assert denied["side_effects"]["external_api_possible"] is False
    assert authorized["side_effects"]["external_api_possible"] is False
    assert denied["semantic_exchange_external_api_allowed"] is False
    assert authorized["semantic_exchange_external_api_allowed"] is True


@pytest.mark.parametrize(
    ("state", "operation", "skill_id", "draft_version"),
    [
        (JobState.TRANSCRIBED, "refine_prepare", "refine-video-transcript", None),
        (JobState.TRANSCRIPT_REFINED, "evidence_prepare", "analyze-video-evidence", None),
        (JobState.EVIDENCE_READY, "author_prepare", "draft-video-report", None),
        (JobState.DRAFTED, "audit_prepare", "independent-content-audit", "v001"),
        (JobState.REVISION_REQUIRED, "revision_prepare", "revise-from-audit", "v001"),
    ],
)
def test_guide_routes_stale_pending_requests_to_transcript_recovery_without_reprocessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: JobState,
    operation: str,
    skill_id: str,
    draft_version: str | None,
) -> None:
    workspace, job = _created_workspace(tmp_path)
    request_path = application._guide_semantic_request_path(
        workspace,
        operation=operation,
        draft_version=draft_version,
    )
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_bytes(b"synthetic frozen request")
    before = (request_path.read_bytes(), request_path.stat().st_mtime_ns)
    monkeypatch.setattr(
        application,
        "load_skill_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(lifecycle="active"),
    )
    monkeypatch.setattr(
        application,
        "_guide_validated_pending_request",
        lambda *_args, **_kwargs: SimpleNamespace(skill=SimpleNamespace(skill_id=skill_id)),
    )
    monkeypatch.setattr(
        application,
        "classify_pending_semantic_request",
        lambda *_args, **_kwargs: PendingSemanticRequestAssessment(
            status=PendingSemanticRequestStatus.STALE_SKILL,
            import_eligible=False,
            migration_required=True,
            reason_code="SKILL_SNAPSHOT_STALE",
            reason="synthetic stale snapshot",
        ),
    )

    result = _guide_operation(
        workspace,
        job,
        [],
        state,
        {"authoring_integrity": {"draft_version": draft_version}},
    )

    assert result["status"] == "migration-required"
    assert result["next_operation"] == "recover"
    assert result["pending_request_status"] == "stale-skill"
    assert result["import_eligible"] is False
    assert result["migration_required"] is True
    assert "skill_path" not in result
    assert result["reuses_verified_transcript"] is True
    assert result["side_effects"] == {
        "network_acquisition": False,
        "normalization": False,
        "ffmpeg": False,
        "asr": False,
        "model_provision": False,
        "external_api_possible": False,
        "workspace_write": True,
    }
    assert (request_path.read_bytes(), request_path.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    ("state", "operation"),
    [
        (JobState.TRANSCRIBED, "refine_prepare"),
        (JobState.TRANSCRIPT_REFINED, "evidence_prepare"),
    ],
)
def test_guide_routes_pre_skill_pending_requests_to_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: JobState,
    operation: str,
) -> None:
    workspace, job = _created_workspace(tmp_path)
    path = application._guide_semantic_request_path(
        workspace,
        operation=operation,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"legacy request")
    monkeypatch.setattr(
        application,
        "load_skill_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(lifecycle="active"),
    )
    monkeypatch.setattr(
        application,
        "_guide_validated_pending_request",
        lambda *_args, **_kwargs: SimpleNamespace(skill=None),
    )

    result = _guide_operation(workspace, job, [], state, {})

    assert result["status"] == "migration-required"
    assert result["pending_request_status"] == "legacy-unbound"
    assert result["next_operation"] == "recover"
    assert "skill_path" not in result


def test_guide_keeps_current_skill_bound_pending_request_on_normal_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, job = _created_workspace(tmp_path)
    path = application._guide_semantic_request_path(
        workspace,
        operation="author_prepare",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"current request")
    monkeypatch.setattr(
        application,
        "load_skill_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(lifecycle="active"),
    )
    monkeypatch.setattr(
        application,
        "_guide_validated_pending_request",
        lambda *_args, **_kwargs: SimpleNamespace(
            skill=SimpleNamespace(skill_id="draft-video-report")
        ),
    )
    monkeypatch.setattr(
        application,
        "classify_pending_semantic_request",
        lambda *_args, **_kwargs: PendingSemanticRequestAssessment(
            status=PendingSemanticRequestStatus.CURRENT,
            import_eligible=True,
            migration_required=False,
            reason_code="SEMANTIC_REQUEST_IMPORTABLE",
            reason="synthetic current snapshot",
        ),
    )

    result = _guide_operation(workspace, job, [], JobState.EVIDENCE_READY, {})

    assert result["status"] == "ready"
    assert result["next_operation"] == "author_prepare"
    assert result["pending_request_status"] == "current"
    assert result["import_eligible"] is True
    assert result["migration_required"] is False
    assert Path(result["skill_path"]).is_absolute()


def test_guide_rejects_pending_request_with_invalid_deterministic_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, job = _created_workspace(tmp_path)
    path = application._guide_semantic_request_path(
        workspace,
        operation="evidence_prepare",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"schema-valid but upstream-stale request")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(
        application,
        "load_skill_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(lifecycle="active"),
    )

    def reject_stale_binding(*_args: object, **_kwargs: object) -> object:
        raise application.state_error(
            "EVIDENCE_REQUEST_STALE",
            "synthetic request belongs to another canonical input",
        )

    monkeypatch.setattr(
        application,
        "_guide_validated_pending_request",
        reject_stale_binding,
    )

    result = _guide_operation(workspace, job, [], JobState.TRANSCRIPT_REFINED, {})

    assert result["status"] == "integrity-error"
    assert result["next_operation"] is None
    assert result["pending_request_status"] == "invalid"
    assert result["import_eligible"] is False
    assert result["migration_required"] is False
    assert result["reason"].startswith("EVIDENCE_REQUEST_STALE")
    assert "skill_path" not in result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_integrity_error_does_not_invent_a_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _job = _created_workspace(tmp_path)
    monkeypatch.setattr(
        application,
        "job_status",
        lambda _path: (
            {
                "authorization": {"present": False, "stale": False},
                "artifact_integrity": {
                    "status": "invalid",
                    "error_code": "ARTIFACT_INTEGRITY_FAILED",
                },
            },
            5,
        ),
    )

    result = job_guide(workspace.root)

    assert result["status"] == "integrity-error"
    assert result["next_operation"] is None
    assert result["command"] is None
    assert result["side_effects"]["workspace_write"] is False


@pytest.mark.parametrize(
    ("route", "patched", "request_case", "request_suffix", "schema_name"),
    [
        (
            ["refine", "prepare"],
            "prepare_transcript_refinement_job",
            SimpleNamespace(
                job_id="guide-job",
                request_fingerprint="0" * 64,
                transcript_segments=[],
                available_source_refs=[],
                skill=SimpleNamespace(skill_id="refine-video-transcript"),
            ),
            "refinement/request.json",
            "refinement-response-manifest.schema.json",
        ),
        (
            ["evidence", "prepare"],
            "prepare_evidence_job",
            SimpleNamespace(
                job_id="guide-job",
                request_fingerprint="0" * 64,
                transcript_segments=[],
                transcript_refs=[],
                frame_refs=[],
                unresolved_ambiguities=[],
                skill=SimpleNamespace(skill_id="analyze-video-evidence"),
            ),
            "evidence/request.json",
            "evidence-response-manifest.schema.json",
        ),
        (
            ["author", "prepare"],
            "prepare_authoring_job",
            SimpleNamespace(
                job_id="guide-job",
                draft_version="v001",
                request_fingerprint="0" * 64,
                skill=SimpleNamespace(skill_id="draft-video-report"),
            ),
            "authoring/requests/author-v001.json",
            "authoring-response-manifest.schema.json",
        ),
        (
            ["audit", "prepare"],
            "prepare_audit_job",
            SimpleNamespace(
                job_id="guide-job",
                target_draft_version="v001",
                request_fingerprint="0" * 64,
                skill=SimpleNamespace(skill_id="independent-content-audit"),
            ),
            "authoring/requests/audit-v001.json",
            "audit-response-manifest.schema.json",
        ),
        (
            ["revision", "prepare"],
            "prepare_revision_job",
            SimpleNamespace(
                job_id="guide-job",
                target_draft_version="v001",
                next_draft_version="v002",
                request_fingerprint="0" * 64,
                skill=SimpleNamespace(skill_id="revise-from-audit"),
            ),
            "authoring/requests/revision-v002.json",
            "revision-response-manifest.schema.json",
        ),
    ],
)
def test_prepare_cli_reports_frozen_exchange_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: list[str],
    patched: str,
    request_case: SimpleNamespace,
    request_suffix: str,
    schema_name: str,
) -> None:
    workspace, _job = _created_workspace(tmp_path)
    monkeypatch.setattr(
        cli,
        patched,
        lambda *_args: SimpleNamespace(request=request_case, reused=False),
    )

    result = CliRunner().invoke(cli.app, [*route, str(workspace.root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert Path(payload["request_path"]).as_posix().endswith(request_suffix)
    assert Path(payload["skill_path"]).name == "SKILL.md"
    assert Path(payload["response_schema_path"]).name == schema_name
