from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from video_evidence_pipeline import cli
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transcription import TranscriptSource
from video_evidence_pipeline.errors import state_error
from video_evidence_pipeline.transcription.container import ContainerAsrBackend
from video_evidence_pipeline.url_workflow import run_url_workflow

_SESSION_ID = "35c4e3ac-6a72-4cbf-8943-7fd62ad1ed21"
_URL_SECRET = "do-not-print-this-ephemeral-value"


def _run_start_with_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str):
    monkeypatch.chdir(tmp_path)
    return CliRunner().invoke(
        cli.app,
        [
            "workflow",
            "start",
            "lecture-job",
            "--source-stdin",
            "--basis",
            "authorized lecture for local study",
            "--confirm-save",
            "--panopto-token-env",
            "VEP_MISSING_PANOPTO_TOKEN",
            "--panopto-tenant-host",
            "tenant.hosted.panopto.com",
            "--task-type",
            "summary",
            "--language",
            "ja",
            "--output-format",
            "markdown",
            "--json",
        ],
        input=source,
    )


def test_workflow_start_reads_embed_url_from_stdin_and_persists_only_session_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = (
        "https://tenant.hosted.panopto.com/Panopto/Pages/Embed.aspx"
        f"?id={_SESSION_ID}&access_token={_URL_SECRET}\n"
    )

    result = _run_start_with_stdin(monkeypatch, tmp_path, source)

    assert result.exit_code == 3, result.output
    workspace = tmp_path / ".local" / "jobs" / "lecture-job"
    job_yaml = (workspace / "job.yaml").read_text(encoding="utf-8")
    assert f"panopto://tenant.hosted.panopto.com/session/{_SESSION_ID}" in job_yaml
    assert _URL_SECRET not in job_yaml
    assert "access_token" not in job_yaml
    assert source.strip() not in result.output
    assert _URL_SECRET not in result.output
    payload = json.loads(result.stdout)
    assert payload["workflow_status"] == "waiting_for_authentication"
    assert payload["job_state"] == "AUTHORIZED"


def test_workflow_start_redacts_invalid_stdin_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _run_start_with_stdin(
        monkeypatch,
        tmp_path,
        f"not-a-panopto-url?token={_URL_SECRET}\n",
    )

    assert result.exit_code == 2
    assert _URL_SECRET not in result.output
    assert "not-a-panopto-url" not in result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "SOURCE_LOCATOR_INVALID"


@pytest.mark.parametrize(
    ("state", "expected_calls"),
    [
        (JobState.AUTHORIZED, ["acquire", "normalize", "transcribe"]),
        (JobState.ACQUIRED, ["acquire", "normalize", "transcribe"]),
        (JobState.NORMALIZED, ["normalize", "transcribe"]),
        (JobState.TRANSCRIBED, ["transcribe"]),
    ],
)
def test_url_workflow_reuses_completed_stages_and_requires_full_gpu_asr(
    monkeypatch: pytest.MonkeyPatch,
    state: JobState,
    expected_calls: list[str],
) -> None:
    calls: list[str] = []
    workspace = SimpleNamespace(root=Path("/virtual/jobs/lecture-job"))
    job = SimpleNamespace(
        job_id="lecture-job",
        source=SimpleNamespace(kind=cli.SourceKind.PANOPTO),
        primary_input=None,
    )
    authorization = {"allow_local_processing": True, "allow_network_acquisition": True}
    monkeypatch.setattr(
        "video_evidence_pipeline.url_workflow._verified_status",
        lambda _path: (workspace, job, {"authorization": authorization}, state),
    )
    monkeypatch.setattr(
        "video_evidence_pipeline.url_workflow.acquire_job",
        lambda *_args: (
            calls.append("acquire") or SimpleNamespace(reused=state is JobState.ACQUIRED)
        ),
    )
    frame = SimpleNamespace(
        frame_id="F000001",
        timestamp_ms=1200,
        artifact=SimpleNamespace(
            relative_storage_path="normalized/frames/abc/F000001.png",
            content_sha256="a" * 64,
        ),
        reasons=[SimpleNamespace(value="periodic")],
    )
    media_manifest = SimpleNamespace(frame_index=SimpleNamespace(records=[frame]))
    monkeypatch.setattr(
        "video_evidence_pipeline.url_workflow.normalize_job",
        lambda *_args: (
            calls.append("normalize")
            or SimpleNamespace(media_manifest=media_manifest, reused=state is JobState.NORMALIZED)
        ),
    )
    asr_backend = SimpleNamespace(
        source=TranscriptSource.ASR,
        selected_device="cuda",
        gpu_index=0,
        gpu_name="Test GPU",
        fallback=False,
        model_repository="Systran/faster-whisper-large-v3",
        model_revision="b" * 40,
        device_policy="cuda",
        total_vram_mib=8192,
        free_vram_at_selection_mib=7000,
        compute_type="float16",
    )

    def transcribe(_path: Path, config, *, backend_override):
        calls.append("transcribe")
        assert config.require_full_asr is True
        assert config.asr_device == "cuda"
        assert isinstance(backend_override, ContainerAsrBackend)
        return SimpleNamespace(
            reused=state is JobState.TRANSCRIBED,
            transcript_manifest=SimpleNamespace(
                configuration=config,
                backends=[asr_backend],
                segment_count=7,
            ),
        )

    monkeypatch.setattr("video_evidence_pipeline.url_workflow.transcribe_job", transcribe)
    if state is JobState.TRANSCRIBED:
        monkeypatch.setattr(
            "video_evidence_pipeline.url_workflow.verify_acquired_bundle",
            lambda *_args: object(),
        )
        monkeypatch.setattr(
            "video_evidence_pipeline.url_workflow.load_normalization_result",
            lambda *_args: SimpleNamespace(media_manifest=media_manifest),
        )

    result = run_url_workflow(
        workspace.root,
        runtime_config=AcquisitionRuntimeConfig(
            panopto_token_env="VEP_TOKEN",
            panopto_tenant_host="tenant.hosted.panopto.com",
        ),
    )

    assert calls == expected_calls
    assert result.state == "TRANSCRIBED"
    assert result.transcript_sources == ["asr"]
    assert result.asr_backend["selected_device"] == "cuda"
    assert result.frame_count == 1


def test_workflow_cli_returns_structured_authentication_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "lecture-job"
    monkeypatch.setattr(
        cli,
        "job_status",
        lambda _path: ({"stored_state": "AUTHORIZED"}, 0),
    )

    def require_authentication(*_args, **_kwargs):
        raise state_error(
            "PANOPTO_REAUTH_REQUIRED",
            "Panopto sign-in or MFA is required before playback can continue",
        )

    monkeypatch.setattr(cli, "run_url_workflow", require_authentication)
    result = CliRunner().invoke(cli.app, ["workflow", "run", str(workspace), "--json"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["workflow_status"] == "waiting_for_authentication"
    assert payload["job_state"] == "AUTHORIZED"
    assert payload["resume_supported"] is True
    assert "rerun this same command" in payload["resume_action"]


def test_workflow_cli_stops_on_actual_viewer_denial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "lecture-job"
    monkeypatch.setattr(
        cli,
        "job_status",
        lambda _path: ({"stored_state": "AUTHORIZED"}, 0),
    )

    def deny_viewer(*_args, **_kwargs):
        raise state_error("PANOPTO_VIEWER_DENIED", "the current viewer cannot access this lecture")

    monkeypatch.setattr(cli, "run_url_workflow", deny_viewer)
    result = CliRunner().invoke(cli.app, ["workflow", "run", str(workspace), "--json"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["workflow_status"] == "stopped"
    assert payload["resume_supported"] is False
    assert payload["resume_action"] is None
