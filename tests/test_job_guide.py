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
from video_evidence_pipeline.domain.state import JobState


def _created_workspace(tmp_path: Path, *, task_type: str = "report", transcript: bool = False):
    source = tmp_path / ("source.txt" if transcript else "source.mp4")
    source.write_text("synthetic", encoding="utf-8")
    workspace, job = initialize_job(
        tmp_path,
        job_id="guide-job",
        source_kind=SourceKind.LOCAL_FILE,
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
    tmp_path: Path, state: JobState, expected: str
) -> None:
    workspace, job = _created_workspace(tmp_path)
    status = {"authoring_integrity": {"draft_version": "v003"}}

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
