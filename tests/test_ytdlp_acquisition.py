from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from video_evidence_pipeline.acquisition.generic_ytdlp import GenericYtdlpAdapter
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionRuntimeConfig,
    BrowserCookieSource,
)
from video_evidence_pipeline.acquisition.ytdlp_runner import hardened_argv, run_ytdlp
from video_evidence_pipeline.application import acquire_job, authorize_job, initialize_job
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.layout import JobWorkspace


def workspace_for(tmp_path: Path, kind: SourceKind = SourceKind.GENERIC_YTDLP) -> JobWorkspace:
    workspace, _ = initialize_job(
        tmp_path,
        job_id="ytdlp-demo",
        source_kind=kind,
        source="https://videos.example.invalid/watch?v=stable-id",
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    return workspace


def output_directory(argv: list[str]) -> Path:
    return Path(argv[argv.index("--paths") + 1])


def successful_runner(captured: dict[str, object], payload: bytes = b"synthetic media"):
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        target = output_directory(argv) / "source.mp4"
        target.write_bytes(payload)
        return subprocess.CompletedProcess(
            argv, 0, stdout=b"secret stdout", stderr=b"secret stderr"
        )

    return run


def test_hardened_ytdlp_argv_is_fixed_and_single_file(tmp_path: Path) -> None:
    runtime = AcquisitionRuntimeConfig(maximum_download_bytes=12345)
    argv = hardened_argv(
        "https://videos.example.invalid/watch?v=stable-id",
        tmp_path,
        runtime,
    )
    assert argv == [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-js-runtimes",
        "--no-update",
        "--no-playlist",
        "--abort-on-error",
        "--no-cache-dir",
        "--no-write-info-json",
        "--no-write-description",
        "--no-write-comments",
        "--no-write-thumbnail",
        "--no-write-subs",
        "--no-write-auto-subs",
        "--no-write-playlist-metafiles",
        "--no-embed-metadata",
        "--no-embed-info-json",
        "--no-embed-thumbnail",
        "--no-embed-subs",
        "--fixup",
        "never",
        "--no-continue",
        "--no-overwrites",
        "--no-netrc",
        "--no-cookies",
        "--quiet",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        "30.0",
        "--max-filesize",
        "12345",
        "--format",
        "best[acodec!=none][vcodec!=none]",
        "--paths",
        str(tmp_path),
        "--output",
        "source.%(ext)s",
        "--",
        "https://videos.example.invalid/watch?v=stable-id",
    ]
    forbidden = {"--exec", "--external-downloader", "--use-postprocessor", "--cookies"}
    assert forbidden.isdisjoint(argv)


def test_browser_cookie_is_only_added_when_explicit(tmp_path: Path) -> None:
    plain = hardened_argv("https://example.invalid/watch", tmp_path, AcquisitionRuntimeConfig())
    cookie = hardened_argv(
        "https://example.invalid/watch",
        tmp_path,
        AcquisitionRuntimeConfig(cookies_from_browser=BrowserCookieSource.FIREFOX),
    )
    assert "--cookies-from-browser" not in plain
    assert cookie[cookie.index("--cookies-from-browser") + 1] == "firefox"


def test_runner_uses_shell_false_and_disables_plugins(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    captured: dict[str, object] = {}
    staged = run_ytdlp(
        "https://videos.example.invalid/watch?v=stable-id",
        workspace,
        AcquisitionRuntimeConfig(maximum_download_bytes=1024),
        adapter_kind=SourceKind.GENERIC_YTDLP,
        runner=successful_runner(captured),
        environment={"PATH": os.environ.get("PATH", ""), "VEP_BOX_ACCESS_TOKEN": "must-not-pass"},
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["YTDLP_NO_PLUGINS"] == "1"
    assert "VEP_BOX_ACCESS_TOKEN" not in environment
    assert staged.path.name == "source.mp4"


@pytest.mark.parametrize("failure", ["missing", "nonzero", "multiple", "sidecar", "symlink"])
def test_runner_failures_are_structured_and_safe(tmp_path: Path, failure: str) -> None:
    workspace = workspace_for(tmp_path)
    synthetic_secret = "synthetic-cookie-secret"

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        directory = output_directory(argv)
        if failure == "missing":
            raise FileNotFoundError(synthetic_secret)
        if failure == "nonzero":
            return subprocess.CompletedProcess(
                argv, 1, stdout=synthetic_secret.encode(), stderr=b"token"
            )
        (directory / "source.mp4").write_bytes(b"media")
        if failure in {"multiple", "sidecar"}:
            (
                directory / ("other.webm" if failure == "multiple" else "source.info.json")
            ).write_bytes(b"sidecar")
        if failure == "symlink":
            (directory / "source.mp4").unlink()
            outside = tmp_path / "outside.mp4"
            outside.write_bytes(b"outside")
            try:
                (directory / "source.mp4").symlink_to(outside)
            except OSError:
                pytest.skip("symlink creation is unavailable")
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=1024),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=runner,
        )
    expected = (
        "YTDLP_NOT_AVAILABLE"
        if failure == "missing"
        else ("YTDLP_ACQUISITION_FAILED" if failure == "nonzero" else "YTDLP_OUTPUT_INVALID")
    )
    assert captured.value.code == expected
    assert synthetic_secret not in captured.value.safe_message


def test_generic_ytdlp_acquisition_publishes_only_verified_output(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic webpage",
        allow_local_processing=True,
        allow_network_acquisition=True,
        allow_external_api=False,
        confirmer_identity="tester",
    )
    runtime = AcquisitionRuntimeConfig(
        cookies_from_browser=BrowserCookieSource.FIREFOX,
        maximum_download_bytes=1024,
    )
    captured: dict[str, object] = {}
    adapter = GenericYtdlpAdapter(runtime, runner=successful_runner(captured))
    result = acquire_job(workspace.root, runtime, adapter)
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.backend == "yt-dlp"
    assert evidence.credential_mode == "browser-cookie"
    assert evidence.http_status_category == "not-observed"
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            workspace.source_manifest_json,
            workspace.acquisition_manifest_json,
            workspace.transitions_jsonl,
        ]
    )
    assert "synthetic media" not in combined
    assert "secret stdout" not in combined
    assert "secret stderr" not in combined
