from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from test_provider_acquisition import initialized_authorized_job, public_resolver
from typer.testing import CliRunner

from video_evidence_pipeline import cli
from video_evidence_pipeline.acquisition.box import BoxAdapter
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transcription import TranscriptSource
from video_evidence_pipeline.errors import state_error
from video_evidence_pipeline.runtime_context import (
    DataContext,
    ResourceContext,
    RuntimeContext,
    use_runtime,
)
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
    query = urlencode({"id": _SESSION_ID, "access_token": _URL_SECRET})
    source = f"https://tenant.hosted.panopto.com/Panopto/Pages/Embed.aspx?{query}\n"

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
    assert payload["authentication"] == {
        "provider": "panopto",
        "action": "configure_credential",
        "actor": "user",
        "methods": [
            {
                "kind": "environment_token",
                "credential_options": ["--panopto-token-env", "--panopto-tenant-host"],
                "browser": None,
                "browser_url": None,
            }
        ],
    }
    assert payload["resume"] == {
        "operation": "workflow.run",
        "workspace": str(workspace),
        "reuse_runtime_options": True,
        "stop_after": "transcribed",
    }
    assert "VEP_MISSING_PANOPTO_TOKEN" not in result.output


def test_workflow_start_redacts_invalid_stdin_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _run_start_with_stdin(
        monkeypatch,
        tmp_path,
        f"not-a-panopto-url?{urlencode({'token': _URL_SECRET})}\n",
    )

    assert result.exit_code == 2
    assert _URL_SECRET not in result.output
    assert "not-a-panopto-url" not in result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "SOURCE_LOCATOR_INVALID"


def test_download_only_resume_reports_bound_installed_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    resources, data, bindings = [tmp_path / name for name in ("resources", "data", "bindings")]
    for root in (resources, data, bindings):
        root.mkdir()
    context = RuntimeContext(
        "test-release", ResourceContext(resources), DataContext(data, bindings)
    )
    args = [
        "workflow",
        "start",
        "auth-job",
        "--source-stdin",
        "--basis",
        "authorized fixture",
        "--confirm-save",
        "--stop-after",
        "acquired",
        "--json",
    ]
    source = f"https://tenant.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id={_SESSION_ID}\n"
    with use_runtime(context):
        for _ in range(2):
            invocation = CliRunner().invoke(cli.app, args, input=source)
            assert invocation.exit_code == 3, invocation.output
            result = json.loads(invocation.stdout)
            assert result["workflow_status"] == "waiting_for_authentication"
            assert result["resume"]["workspace"] == str(data / "jobs" / "auth-job")
    assert len(list((data / "jobs").iterdir())) == 1


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


def test_browser_authentication_link_is_derived_and_can_be_replayed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    source = (
        f"https://tenant.hosted.panopto.com/Panopto/Pages/Embed.aspx?"
        f"{urlencode({'id': _SESSION_ID, 'access_token': _URL_SECRET})}"
    )

    def require_authentication(*_args):
        error = state_error("PANOPTO_REAUTH_REQUIRED", "browser sign-in required")
        error.authentication_method = "browser_session"
        raise error

    monkeypatch.setattr("video_evidence_pipeline.url_workflow.acquire_job", require_authentication)
    args = [
        "workflow",
        "start",
        "--source-stdin",
        "--basis",
        "authorized fixture",
        "--confirm-save",
        "--cookies-from-browser",
        "firefox",
        "--stop-after",
        "acquired",
        "--json",
    ]
    first = CliRunner().invoke(cli.app, args, input=source + "\n")
    assert first.exit_code == 3, first.output
    wait = json.loads(first.stdout)
    assert wait["authentication"]["methods"] == [
        {
            "kind": "browser_session",
            "credential_options": ["--cookies-from-browser"],
            "browser": "firefox",
            "browser_url": (
                f"https://tenant.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id={_SESSION_ID}"
            ),
        }
    ]
    assert _URL_SECRET not in first.output
    assert wait["resume"]["stop_after"] == "acquired"

    def no_asr(*_args):
        raise AssertionError("download-only workflow must not start ASR")

    def complete_acquisition(path, runtime):
        assert path == Path(wait["resume"]["workspace"])
        assert runtime.cookies_from_browser.value == "firefox"
        artifact = SimpleNamespace(relative_storage_path="source/raw/synthetic.mp4")
        return SimpleNamespace(
            source_manifest=SimpleNamespace(artifact_record=SimpleNamespace(artifact=artifact)),
            reused=False,
        )

    monkeypatch.setattr("video_evidence_pipeline.url_workflow.require_source_asr", no_asr)
    monkeypatch.setattr("video_evidence_pipeline.url_workflow.acquire_job", complete_acquisition)
    second = CliRunner().invoke(
        cli.app,
        [
            "workflow",
            "run",
            wait["resume"]["workspace"],
            "--cookies-from-browser",
            "firefox",
            "--stop-after",
            "acquired",
            "--json",
        ],
    )
    assert second.exit_code == 0, second.output
    completed = json.loads(second.stdout)
    assert completed["job_id"] == wait["job_id"]
    assert completed["state"] == "ACQUIRED"
    assert completed["artifacts"]["raw_media"] == "source/raw/synthetic.mp4"
    assert completed["artifacts"]["transcript_segments"] is None


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
    assert "authentication" not in payload
    assert "resume" not in payload


@pytest.mark.parametrize("workflow", [False, True], ids=["acquire", "workflow-run"])
def test_box_api_authentication_handoff_reuses_authorized_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workflow: bool
) -> None:
    workspace = initialized_authorized_job(tmp_path, SourceKind.BOX, "123")
    monkeypatch.setenv("VEP_BOX_AUTH_TEST", _URL_SECRET)
    body = b"synthetic Box source bytes"
    request_count = 0

    def box_provider(request):
        nonlocal request_count
        request_count += 1
        assert request.headers["authorization"] == f"Bearer {_URL_SECRET}"
        if request_count == 1:
            return httpx.Response(401)
        if request.url.path == "/2.0/files/123":
            return httpx.Response(
                200, json={"id": "123", "type": "file", "size": len(body), "name": "test.mp4"}
            )
        assert request.url.path == "/2.0/files/123/content"
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=body)

    flags = ["--box-token-env", "VEP_BOX_AUTH_TEST", "--json"]
    args = (
        ["workflow", "run", str(workspace.root), "--stop-after", "acquired", *flags]
        if workflow
        else ["acquire", str(workspace.root), *flags]
    )
    with httpx.Client(transport=httpx.MockTransport(box_provider)) as client:
        monkeypatch.setattr(
            "video_evidence_pipeline.application.adapter_for",
            lambda _kind, runtime: BoxAdapter(runtime, client=client, resolver=public_resolver),
        )
        first = CliRunner().invoke(cli.app, args)
        assert first.exit_code == 4, first.output
        wait = json.loads(first.stdout)
        assert wait["workflow_status"] == "waiting_for_authentication"
        assert wait["error"]["code"] == "BOX_REAUTH_REQUIRED"
        assert wait["resume"]["operation"] == ("workflow.run" if workflow else "acquire")
        assert wait["resume"]["workspace"] == str(workspace.root)
        assert wait["authentication"]["provider"] == "box"
        assert wait["authentication"]["action"] == "authenticate"
        assert wait["authentication"]["methods"][0]["credential_options"] == ["--box-token-env"]
        assert _URL_SECRET not in first.output
        assert not workspace.source_manifest_json.exists()

        second = CliRunner().invoke(cli.app, args)
        assert second.exit_code == 0, second.output
        assert json.loads(second.stdout)["state"] == "ACQUIRED"
        third = CliRunner().invoke(cli.app, args)
        assert third.exit_code == 0, third.output
        reused = json.loads(third.stdout)["reused"]
        assert (reused["acquisition"] if workflow else reused) is True
        assert request_count == 3
