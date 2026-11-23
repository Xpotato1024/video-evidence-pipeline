"""Exercise real job transitions, FFmpeg and artifact reuse around simulated external services."""

import json
from pathlib import Path

import httpx
from test_asr_runtime_policy import (
    _caps,
    _profile,
    _profiled_backend_from_selection,
    _worker_request,
)
from test_media_normalization import _synthetic_source
from test_provider_acquisition import PANOPTO_VIEWER, SESSION_ID, public_resolver
from typer.testing import CliRunner

from video_evidence_pipeline import application, cli
from video_evidence_pipeline.acquisition.panopto import PanoptoAdapter
from video_evidence_pipeline.domain.transcription import (
    AsrSegment,
    AsrWorkerResponse,
    TranscriptConfidence,
)
from video_evidence_pipeline.errors import configuration_error
from video_evidence_pipeline.transcription import container
from video_evidence_pipeline.transcription.device import GpuDevice, select_device


def test_resume_same_lecture_after_login_and_gpu_interruptions(tmp_path: Path, monkeypatch) -> None:
    source = _synthetic_source(tmp_path, audio=True, subtitle=False)
    selection = select_device(_profile(), "cuda", _caps(GpuDevice(0, "test GPU", 8192, 7000)))
    response = AsrWorkerResponse(
        backend=_profiled_backend_from_selection(_worker_request("cuda"), selection),
        segments=[
            AsrSegment(
                start_ms=0,
                end_ms=1000,
                raw_text="synthetic speech",
                backend_segment_id="s1",
                confidence=TranscriptConfidence(kind="backend_metric", avg_logprob=-0.1),
            )
        ],
    )
    requests = []

    def provider(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(401)
        if request.url.path.endswith(SESSION_ID):
            return httpx.Response(
                200,
                json={
                    "Id": SESSION_ID,
                    "Duration": 3,
                    "Name": "lecture.mp4",
                    "Urls": {"DownloadUrl": "https://cdn.example/lecture.mp4"},
                },
            )
        assert request.url.host == "cdn.example" and "authorization" not in request.headers
        return httpx.Response(
            200, headers={"Content-Type": "video/mp4"}, content=source.read_bytes()
        )

    attempts = []

    def worker(_root, audio_path, intervals, config):
        assert audio_path.is_file() and config.asr_device == "cuda" and config.require_full_asr
        assert len(intervals) == 1 and intervals[0].start_ms == 0
        attempts.append(audio_path)
        if len(attempts) == 1:
            raise configuration_error("ASR_DEVICE_UNAVAILABLE", "synthetic GPU interruption")
        return response

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VEP_LECTURE_TOKEN", "synthetic")
    monkeypatch.setattr(container, "run_asr_worker", worker)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        monkeypatch.setattr(
            application,
            "adapter_for",
            lambda _source, runtime: PanoptoAdapter(
                runtime, client=client, resolver=public_resolver
            ),
        )
        args = [
            "workflow",
            "start",
            "--source-stdin",
            "--basis",
            "authorized fixture",
            "--confirm-save",
            "--panopto-token-env",
            "VEP_LECTURE_TOKEN",
            "--panopto-tenant-host",
            "tenant.hosted.panopto.com",
            "--json",
        ]

        def run():
            invocation = CliRunner().invoke(cli.app, args, input=PANOPTO_VIEWER + "\n")
            assert invocation.stdout, repr(invocation.exception)
            return json.loads(invocation.stdout)

        login = run()
        assert login["error"]["code"] == "PANOPTO_REAUTH_REQUIRED"
        assert login["workflow_status"] == "waiting_for_authentication"
        unavailable = run()
        assert unavailable["error"]["code"] == "ASR_DEVICE_UNAVAILABLE", unavailable
        assert unavailable["job_state"] == "NORMALIZED"
        complete = run()
        assert complete["ok"] and complete["state"] == "TRANSCRIBED", complete
        workspace = Path(complete["workspace_root"])
        assert workspace.name.startswith("panopto-")
        assert list(workspace.parent.iterdir()) == [workspace]
        for key in ("raw_media", "transcript_segments", "frame_index"):
            assert (workspace / complete["artifacts"][key]).is_file()
        assert complete["frame_count"] > 0
        assert run()["reused"] == {
            "acquisition": True,
            "normalization": True,
            "transcription": True,
        }
        assert len(requests) == 3 and len(attempts) == 2
