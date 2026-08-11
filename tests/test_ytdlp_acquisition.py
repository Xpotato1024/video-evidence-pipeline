from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from yt_dlp.options import create_parser

from video_evidence_pipeline.acquisition import ytdlp_runner
from video_evidence_pipeline.acquisition.generic_ytdlp import GenericYtdlpAdapter
from video_evidence_pipeline.acquisition.https_proxy import validating_https_proxy
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
    BrowserCookieSource,
)
from video_evidence_pipeline.acquisition.staged import load_network_result
from video_evidence_pipeline.acquisition.ytdlp_runner import hardened_argv, run_ytdlp
from video_evidence_pipeline.application import acquire_job, authorize_job, initialize_job
from video_evidence_pipeline.domain.job import SourceKind, load_job_yaml
from video_evidence_pipeline.domain.media import ToolIdentity
from video_evidence_pipeline.errors import VepError, configuration_error, io_error
from video_evidence_pipeline.storage.layout import JobWorkspace


def public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    ("now", "expected_code"),
    [(2.0, "NETWORK_TIMEOUT"), (0.5, "YTDLP_TOOLCHAIN_INVALID")],
)
def test_tool_discovery_timeout_preserves_absolute_deadline_classification(
    monkeypatch: pytest.MonkeyPatch,
    now: float,
    expected_code: str,
) -> None:
    class TimeoutRunner:
        def discover_identities(self, **_kwargs: object) -> None:
            raise io_error("MEDIA_TOOL_TIMEOUT", "synthetic media tool timeout")

    monkeypatch.setattr(ytdlp_runner, "FFmpegRunner", TimeoutRunner)
    deadline = AcquisitionDeadline(expires_at=1.0, monotonic=lambda: now)

    with pytest.raises(VepError) as captured:
        ytdlp_runner.default_ytdlp_toolchain(deadline)

    assert captured.value.code == expected_code


@pytest.mark.parametrize(
    ("now", "expected_code"),
    [(2.0, "NETWORK_TIMEOUT"), (0.5, "YTDLP_OUTPUT_INVALID")],
)
def test_final_probe_timeout_preserves_absolute_deadline_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: float,
    expected_code: str,
) -> None:
    def timeout_probe(*_args: object, **_kwargs: object) -> None:
        raise io_error("MEDIA_TOOL_TIMEOUT", "synthetic media probe timeout")

    monkeypatch.setattr(ytdlp_runner, "probe_media", timeout_probe)
    identity = ToolIdentity(
        name="ffmpeg",
        version="synthetic",
        executable_sha256="0" * 64,
        capabilities=[],
    )
    toolchain = ytdlp_runner._PinnedYtdlpToolchain(
        runner=object(),  # type: ignore[arg-type]
        ffmpeg_identity=identity,
        ffprobe_identity=identity.model_copy(update={"name": "ffprobe"}),
        ffmpeg_location=tmp_path,
    )
    deadline = AcquisitionDeadline(expires_at=1.0, monotonic=lambda: now)

    with pytest.raises(VepError) as captured:
        toolchain.validate_final(tmp_path / "synthetic.mp4", deadline)

    assert captured.value.code == expected_code


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


def lineage_path(argv: list[str]) -> Path:
    return Path(argv[argv.index("--print-to-file") + 2])


def write_lineage(argv: list[str], *, adaptive: bool = False) -> None:
    payload = {
        "final": {
            "format_id": "137+140" if adaptive else "22",
            "vcodec": "avc1.640028",
            "acodec": "mp4a.40.2",
            "protocol": "https",
        },
        "components": (
            [
                {
                    "format_id": "137",
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "protocol": "http_dash_segments",
                },
                {
                    "format_id": "140",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "protocol": "http_dash_segments",
                },
            ]
            if adaptive
            else []
        ),
    }
    lineage_path(argv).write_text(json.dumps(payload) + "\n", encoding="utf-8")


class FakeYtdlpToolchain:
    def __init__(self, root: Path) -> None:
        self.ffmpeg_location = root.resolve()
        self.ffmpeg_identity = ToolIdentity(
            name="ffmpeg",
            version="8.1.2-synthetic",
            executable_sha256="1" * 64,
            capabilities=[],
        )
        self.validated: list[Path] = []
        self.revalidations = 0

    def validate_final(self, path: Path, _deadline: object) -> None:
        self.validated.append(path)

    def revalidate(self) -> None:
        self.revalidations += 1


def fake_toolchain_factory(tmp_path: Path):
    toolchain = FakeYtdlpToolchain(tmp_path)
    return toolchain, lambda _deadline: toolchain


def required_ffmpeg_bin() -> Path:
    configured = os.environ.get("VEP_FFMPEG_BIN_DIR")
    if configured:
        return Path(configured)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is not None and ffprobe is not None:
        ffmpeg_path = Path(ffmpeg).resolve()
        ffprobe_path = Path(ffprobe).resolve()
        if ffmpeg_path.parent == ffprobe_path.parent:
            return ffmpeg_path.parent
    if os.environ.get("VEP_REQUIRE_FFMPEG_TESTS") == "1":
        pytest.fail("VEP_REQUIRE_FFMPEG_TESTS=1 but a common FFmpeg bin directory is unavailable")
    pytest.skip("FFmpeg integration toolchain is unavailable")


def successful_runner(captured: dict[str, object], payload: bytes = b"synthetic media"):
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        target = output_directory(argv) / "source.mp4"
        target.write_bytes(payload)
        write_lineage(argv)
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
        proxy_url="http://127.0.0.1:43210",
        ffmpeg_location=tmp_path,
        lineage_path=tmp_path / "lineage.jsonl",
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
        "--no-keep-video",
        "--no-video-multistreams",
        "--no-audio-multistreams",
        "--no-cookies",
        "--proxy",
        "http://127.0.0.1:43210",
        "--quiet",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        "30.0",
        "--max-filesize",
        "12345",
        "--ffmpeg-location",
        str(tmp_path),
        "--format",
        "bv*+ba/b",
        "--print-to-file",
        'after_move:{"final":%(.{format_id,vcodec,acodec,protocol})j,"components":%(requested_formats.:.{format_id,vcodec,acodec,protocol})j}',
        str(tmp_path / "lineage.jsonl"),
        "--paths",
        str(tmp_path),
        "--output",
        "source.%(ext)s",
        "--",
        "https://videos.example.invalid/watch?v=stable-id",
    ]
    forbidden = {
        "--cookies",
        "--exec",
        "--external-downloader",
        "--extractor-args",
        "--netrc",
        "--netrc-cmd",
        "--netrc-location",
        "--use-postprocessor",
    }
    assert forbidden.isdisjoint(argv)


@pytest.mark.parametrize("cookie_source", [None, BrowserCookieSource.FIREFOX])
def test_hardened_argv_is_accepted_by_pinned_ytdlp_parser(
    tmp_path: Path,
    cookie_source: BrowserCookieSource | None,
) -> None:
    locator = "https://videos.example.invalid/watch?v=stable-id"
    argv = hardened_argv(
        locator,
        tmp_path,
        AcquisitionRuntimeConfig(
            maximum_download_bytes=12345,
            cookies_from_browser=cookie_source,
        ),
        proxy_url="http://127.0.0.1:43210",
        ffmpeg_location=tmp_path,
        lineage_path=tmp_path / "lineage.jsonl",
    )

    parser = create_parser()
    options, positional = parser.parse_args(argv[3:])

    assert positional == [locator]
    assert options.ignoreconfig is True
    assert options.usenetrc is False


def test_browser_cookie_is_only_added_when_explicit(tmp_path: Path) -> None:
    plain = hardened_argv(
        "https://example.invalid/watch",
        tmp_path,
        AcquisitionRuntimeConfig(),
        proxy_url="http://127.0.0.1:43210",
        ffmpeg_location=tmp_path,
        lineage_path=tmp_path / "plain-lineage.jsonl",
    )
    cookie = hardened_argv(
        "https://example.invalid/watch",
        tmp_path,
        AcquisitionRuntimeConfig(cookies_from_browser=BrowserCookieSource.FIREFOX),
        proxy_url="http://127.0.0.1:43210",
        ffmpeg_location=tmp_path,
        lineage_path=tmp_path / "cookie-lineage.jsonl",
    )
    assert "--cookies-from-browser" not in plain
    assert cookie[cookie.index("--cookies-from-browser") + 1] == "firefox"


def test_runner_uses_shell_false_and_disables_plugins(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    captured: dict[str, object] = {}
    toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)
    staged = run_ytdlp(
        "https://videos.example.invalid/watch?v=stable-id",
        workspace,
        AcquisitionRuntimeConfig(maximum_download_bytes=1024),
        adapter_kind=SourceKind.GENERIC_YTDLP,
        runner=successful_runner(captured),
        environment={"PATH": os.environ.get("PATH", ""), "VEP_BOX_ACCESS_TOKEN": "must-not-pass"},
        resolver=public_resolver,
        toolchain_factory=toolchain_factory,
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["cwd"] == staged.cleanup_root
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["YTDLP_NO_PLUGINS"] == "1"
    assert "VEP_BOX_ACCESS_TOKEN" not in environment
    assert staged.path.name == "source.mp4"
    assert staged.evidence.acquisition_mode == "progressive"
    assert [item.format_id for item in staged.evidence.selected_formats] == ["22"]
    assert staged.evidence.merger is None
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--proxy") + 1].startswith("http://127.0.0.1:")
    assert argv[argv.index("--ffmpeg-location") + 1] == str(tmp_path.resolve())
    assert toolchain.validated == [staged.path]
    assert toolchain.revalidations == 2


def test_production_runner_stops_process_when_streaming_output_exceeds_limit(
    tmp_path: Path,
) -> None:
    workspace = workspace_for(tmp_path)
    captured: dict[str, object] = {}

    class FakeProcess:
        terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> int:
            assert timeout == 1
            return 0

        def kill(self) -> None:
            raise AssertionError("terminate should be sufficient")

    process = FakeProcess()
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    def process_factory(argv: list[str], **kwargs: object):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        output_directory(argv).joinpath("source.mp4").write_bytes(b"x" * 9)
        return process

    with pytest.raises(VepError) as error:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=8),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            resolver=public_resolver,
            process_factory=process_factory,
            toolchain_factory=toolchain_factory,
        )
    assert error.value.code == "SOURCE_TOO_LARGE"
    assert process.terminated is True
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "nonzero",
        "multiple",
        "sidecar",
        "residual-component",
        "directory",
        "zero-byte",
        "missing-lineage",
        "symlink",
    ],
)
def test_runner_failures_are_structured_and_safe(tmp_path: Path, failure: str) -> None:
    workspace = workspace_for(tmp_path)
    synthetic_secret = "synthetic-cookie-secret"
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        directory = output_directory(argv)
        if failure == "missing":
            raise FileNotFoundError(synthetic_secret)
        if failure == "nonzero":
            return subprocess.CompletedProcess(
                argv, 1, stdout=synthetic_secret.encode(), stderr=b"token"
            )
        (directory / "source.mp4").write_bytes(b"" if failure == "zero-byte" else b"media")
        if failure != "missing-lineage":
            write_lineage(argv)
        if failure in {"multiple", "sidecar"}:
            (
                directory / ("other.webm" if failure == "multiple" else "source.info.json")
            ).write_bytes(b"sidecar")
        if failure == "residual-component":
            (directory / "source.f137.mp4").write_bytes(b"video")
        if failure == "directory":
            (directory / "unexpected").mkdir()
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
            resolver=public_resolver,
            toolchain_factory=toolchain_factory,
        )
    expected = (
        "YTDLP_NOT_AVAILABLE"
        if failure == "missing"
        else ("YTDLP_ACQUISITION_FAILED" if failure == "nonzero" else "YTDLP_OUTPUT_INVALID")
    )
    assert captured.value.code == expected
    assert synthetic_secret not in captured.value.safe_message
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert list(workspace.safe_path("source/staging").iterdir()) == []


def test_merged_final_over_limit_is_rejected_and_cleaned(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        (output_directory(argv) / "source.mp4").write_bytes(b"x" * 9)
        write_lineage(argv, adaptive=True)
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=8),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=runner,
            resolver=public_resolver,
            toolchain_factory=toolchain_factory,
        )
    assert captured.value.code == "SOURCE_TOO_LARGE"
    assert list(workspace.safe_path("source/staging").iterdir()) == []


@pytest.mark.parametrize("timed_out", [False, True])
def test_adaptive_merge_failure_is_typed_redacted_and_cleaned(
    tmp_path: Path, timed_out: bool
) -> None:
    workspace = workspace_for(tmp_path)
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)
    synthetic_secret = "synthetic-private-marker"

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        directory = output_directory(argv)
        (directory / "source.f137.mp4").write_bytes(b"video")
        (directory / "source.f140.m4a").write_bytes(b"audio")
        if timed_out:
            raise subprocess.TimeoutExpired(argv, 1, stderr=synthetic_secret.encode())
        return subprocess.CompletedProcess(argv, 1, stderr=synthetic_secret.encode())

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=1024),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=runner,
            resolver=public_resolver,
            toolchain_factory=toolchain_factory,
        )
    assert captured.value.code == "YTDLP_MERGE_FAILED"
    assert synthetic_secret not in captured.value.safe_message
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert list(workspace.safe_path("source/staging").iterdir()) == []


def test_adaptive_formats_record_merge_lineage_and_one_final(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        (output_directory(argv) / "source.mp4").write_bytes(b"merged synthetic media")
        write_lineage(argv, adaptive=True)
        return subprocess.CompletedProcess(argv, 0)

    staged = run_ytdlp(
        "https://videos.example.invalid/watch?v=stable-id",
        workspace,
        AcquisitionRuntimeConfig(maximum_download_bytes=1024),
        adapter_kind=SourceKind.GENERIC_YTDLP,
        runner=runner,
        resolver=public_resolver,
        toolchain_factory=toolchain_factory,
    )

    assert staged.evidence.acquisition_mode == "adaptive-merged"
    assert [item.format_id for item in staged.evidence.selected_formats] == ["137", "140"]
    assert staged.evidence.selected_formats[0].protocol == "http_dash_segments"
    assert staged.evidence.merger == toolchain.ffmpeg_identity
    assert [entry.name for entry in staged.cleanup_root.iterdir()] == ["source.mp4"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "final": {
                    "format_id": "unsafe/private-format",
                    "vcodec": "avc1",
                    "acodec": "aac",
                    "protocol": "https",
                },
                "components": [],
            },
            "YTDLP_OUTPUT_INVALID",
        ),
        (
            {
                "final": {
                    "format_id": "137+140",
                    "vcodec": "avc1",
                    "acodec": "aac",
                    "protocol": "https",
                },
                "components": [
                    {
                        "format_id": "137",
                        "vcodec": "avc1",
                        "acodec": "none",
                        "protocol": "https",
                    }
                ],
            },
            "YTDLP_FORMAT_UNAVAILABLE",
        ),
    ],
)
def test_lineage_rejects_sensitive_or_unbounded_composition(
    tmp_path: Path,
    payload: dict[str, object],
    expected: str,
) -> None:
    workspace = workspace_for(tmp_path)
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        (output_directory(argv) / "source.mp4").write_bytes(b"media")
        lineage_path(argv).write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=1024),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=runner,
            resolver=public_resolver,
            toolchain_factory=toolchain_factory,
        )
    assert captured.value.code == expected
    assert "synthetic-secret" not in captured.value.safe_message
    assert list(workspace.safe_path("source/staging").iterdir()) == []


@pytest.mark.parametrize("component_name", ["source.f137.mp4", "source.f140.m4a"])
def test_streaming_component_limit_terminates_and_cleans_up(
    tmp_path: Path, component_name: str
) -> None:
    workspace = workspace_for(tmp_path)
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    class FakeProcess:
        terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> int:
            assert timeout == 1
            return 0

        def kill(self) -> None:
            raise AssertionError("terminate should be sufficient")

    process = FakeProcess()

    def process_factory(argv: list[str], **_kwargs: object) -> FakeProcess:
        (output_directory(argv) / component_name).write_bytes(b"x" * 9)
        return process

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=8),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            resolver=public_resolver,
            process_factory=process_factory,
            toolchain_factory=toolchain_factory,
        )
    assert captured.value.code == "SOURCE_TOO_LARGE"
    assert process.terminated is True
    assert list(workspace.safe_path("source/staging").iterdir()) == []


def test_cumulative_staging_limit_terminates_and_cleans_up(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    class FakeProcess:
        terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("terminate should be sufficient")

    process = FakeProcess()

    def process_factory(argv: list[str], **_kwargs: object) -> FakeProcess:
        directory = output_directory(argv)
        for index in range(4):
            (directory / f"bounded-{index}.part").write_bytes(b"x" * 8)
        return process

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=8),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            resolver=public_resolver,
            process_factory=process_factory,
            toolchain_factory=toolchain_factory,
        )
    assert captured.value.code == "SOURCE_TOO_LARGE"
    assert process.terminated is True
    assert list(workspace.safe_path("source/staging").iterdir()) == []


def test_timeout_kills_process_and_cleans_partial_output(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    class FakeProcess:
        terminated = False
        killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> int:
            if not self.killed:
                raise subprocess.TimeoutExpired("yt-dlp", timeout)
            return 0

        def kill(self) -> None:
            self.killed = True

    process = FakeProcess()

    def process_factory(argv: list[str], **_kwargs: object) -> FakeProcess:
        (output_directory(argv) / "source.part").write_bytes(b"partial")
        return process

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(
                maximum_download_bytes=1024,
                maximum_download_seconds=1,
            ),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            resolver=public_resolver,
            process_factory=process_factory,
            toolchain_factory=toolchain_factory,
            monotonic=iter([0.0, 0.0, 2.0]).__next__,
        )
    assert captured.value.code == "NETWORK_TIMEOUT"
    assert process.terminated is True
    assert process.killed is True
    assert list(workspace.safe_path("source/staging").iterdir()) == []


def test_replaced_ffmpeg_identity_fails_closed_and_cleans_output(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    def invalid_revalidation() -> None:
        raise configuration_error("YTDLP_TOOLCHAIN_INVALID", "synthetic replacement")

    toolchain.revalidate = invalid_revalidation  # type: ignore[method-assign]

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=1024),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=successful_runner({}),
            resolver=public_resolver,
            toolchain_factory=toolchain_factory,
        )
    assert captured.value.code == "YTDLP_TOOLCHAIN_INVALID"
    assert list(workspace.safe_path("source/staging").iterdir()) == []


def test_missing_configured_ffmpeg_fails_before_ytdlp_and_does_not_use_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = workspace_for(tmp_path)
    missing = tmp_path / "missing-pinned-bin"
    rogue = tmp_path / "rogue-bin"
    rogue.mkdir()
    (rogue / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).write_bytes(b"rogue")
    monkeypatch.setenv("VEP_FFMPEG_BIN_DIR", str(missing))
    monkeypatch.setenv("PATH", str(rogue))
    called = False

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess([], 0)

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=1024),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=runner,
            resolver=public_resolver,
        )
    assert captured.value.code == "YTDLP_TOOLCHAIN_INVALID"
    assert called is False
    assert list(workspace.safe_path("source/staging").iterdir()) == []


@pytest.mark.parametrize("adaptive", [False, True], ids=["progressive", "adaptive-merged"])
def test_synthetic_av_uses_validated_ffmpeg_and_publishes_one_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adaptive: bool,
) -> None:
    ffmpeg_bin = required_ffmpeg_bin()
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ffmpeg = ffmpeg_bin / ffmpeg_name
    rogue_bin = tmp_path / "rogue-bin"
    rogue_bin.mkdir()
    (rogue_bin / ffmpeg_name).write_bytes(b"rogue ffmpeg must not run")
    monkeypatch.setenv("VEP_FFMPEG_BIN_DIR", str(ffmpeg_bin))
    monkeypatch.setenv("PATH", str(rogue_bin))
    workspace = workspace_for(tmp_path)
    captured: dict[str, object] = {}

    def invoke(arguments: list[str]) -> None:
        subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        directory = output_directory(argv)
        if adaptive:
            video = directory / "source.f137.mp4"
            audio = directory / "source.f140.m4a"
            invoke(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=64x64:rate=10",
                    "-t",
                    "0.4",
                    "-c:v",
                    "mpeg4",
                    "-an",
                    "-y",
                    str(video),
                ]
            )
            invoke(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:sample_rate=48000",
                    "-t",
                    "0.4",
                    "-c:a",
                    "aac",
                    "-vn",
                    "-y",
                    str(audio),
                ]
            )
            invoke(
                [
                    "-i",
                    str(video),
                    "-i",
                    str(audio),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c",
                    "copy",
                    "-y",
                    str(directory / "source.mp4"),
                ]
            )
            video.unlink()
            audio.unlink()
        else:
            invoke(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=64x64:rate=10",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:sample_rate=48000",
                    "-t",
                    "0.4",
                    "-c:v",
                    "mpeg4",
                    "-c:a",
                    "aac",
                    "-shortest",
                    "-y",
                    str(directory / "source.mp4"),
                ]
            )
        write_lineage(argv, adaptive=adaptive)
        return subprocess.CompletedProcess(argv, 0)

    staged = run_ytdlp(
        "https://videos.example.invalid/watch?v=stable-id",
        workspace,
        AcquisitionRuntimeConfig(maximum_download_bytes=1024 * 1024),
        adapter_kind=SourceKind.GENERIC_YTDLP,
        runner=runner,
        resolver=public_resolver,
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert Path(argv[argv.index("--ffmpeg-location") + 1]) == ffmpeg_bin
    assert [entry.name for entry in staged.cleanup_root.iterdir()] == ["source.mp4"]
    assert staged.evidence.acquisition_mode == ("adaptive-merged" if adaptive else "progressive")
    if adaptive:
        assert staged.evidence.merger is not None
        assert staged.evidence.merger.name == "ffmpeg"
        if "8.1.2" in str(ffmpeg_bin):
            assert "8.1.2" in staged.evidence.merger.version
    else:
        assert staged.evidence.merger is None


def test_final_media_without_audio_fails_probe_and_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg_bin = required_ffmpeg_bin()
    ffmpeg = ffmpeg_bin / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    monkeypatch.setenv("VEP_FFMPEG_BIN_DIR", str(ffmpeg_bin))
    workspace = workspace_for(tmp_path)

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=64x64:rate=10",
                "-t",
                "0.4",
                "-c:v",
                "mpeg4",
                "-an",
                "-y",
                str(output_directory(argv) / "source.mp4"),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        write_lineage(argv)
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(VepError) as captured:
        run_ytdlp(
            "https://videos.example.invalid/watch?v=stable-id",
            workspace,
            AcquisitionRuntimeConfig(maximum_download_bytes=1024 * 1024),
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=runner,
            resolver=public_resolver,
        )
    assert captured.value.code == "YTDLP_OUTPUT_INVALID"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert list(workspace.safe_path("source/staging").iterdir()) == []


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
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)
    adapter = GenericYtdlpAdapter(
        runtime,
        runner=successful_runner(captured),
        resolver=public_resolver,
        toolchain_factory=toolchain_factory,
    )
    result = acquire_job(workspace.root, runtime, adapter)
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.backend == "yt-dlp"
    assert evidence.credential_mode == "browser-cookie"
    assert evidence.http_status_category == "not-observed"
    assert evidence.final_host is None
    assert evidence.redirect_count is None
    assert evidence.acquisition_mode == "progressive"
    assert [item.format_id for item in evidence.selected_formats] == ["22"]
    assert evidence.merger is None
    assert evidence.warnings == ["yt-dlp final host and redirect count were not observed"]
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
    assert "https://videos.example.invalid" not in combined


def test_adaptive_publication_persists_safe_lineage_and_final_identity(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic adaptive fixture",
        allow_local_processing=True,
        allow_network_acquisition=True,
        allow_external_api=False,
        confirmer_identity="tester",
    )
    toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        (output_directory(argv) / "source.mp4").write_bytes(b"adaptive merged media")
        write_lineage(argv, adaptive=True)
        return subprocess.CompletedProcess(argv, 0)

    runtime = AcquisitionRuntimeConfig(maximum_download_bytes=1024)
    result = acquire_job(
        workspace.root,
        runtime,
        GenericYtdlpAdapter(
            runtime,
            runner=runner,
            resolver=public_resolver,
            toolchain_factory=toolchain_factory,
        ),
    )
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.acquisition_mode == "adaptive-merged"
    assert [item.format_id for item in evidence.selected_formats] == ["137", "140"]
    assert evidence.merger == toolchain.ffmpeg_identity
    assert result.source_manifest.artifact_record.artifact.size_bytes == len(
        b"adaptive merged media"
    )
    persisted = workspace.source_manifest_json.read_text(encoding="utf-8")
    assert "http_dash_segments" in persisted
    assert "executable_sha256" in persisted
    assert "https://videos.example.invalid" not in persisted
    assert "media_url" not in persisted


def test_legacy_progressive_manifest_remains_recoverable(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path)
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic legacy fixture",
        allow_local_processing=True,
        allow_network_acquisition=True,
        allow_external_api=False,
        confirmer_identity="tester",
    )
    _toolchain, toolchain_factory = fake_toolchain_factory(tmp_path)
    runtime = AcquisitionRuntimeConfig(maximum_download_bytes=1024)
    acquire_job(
        workspace.root,
        runtime,
        GenericYtdlpAdapter(
            runtime,
            runner=successful_runner({}),
            resolver=public_resolver,
            toolchain_factory=toolchain_factory,
        ),
    )
    payload = json.loads(workspace.source_manifest_json.read_text(encoding="utf-8"))
    evidence = payload["acquisition_evidence"]
    for key in ("acquisition_mode", "selected_formats", "merger"):
        evidence.pop(key)
    workspace.source_manifest_json.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    recovered = load_network_result(workspace, load_job_yaml(workspace.job_yaml))
    assert recovered.reused is True
    assert recovered.source_manifest.acquisition_evidence is not None
    assert recovered.source_manifest.acquisition_evidence.acquisition_mode is None


def test_validating_proxy_rejects_private_connect_destination() -> None:
    connector_called = False

    def connector(_host: str, _port: int, _timeout: float) -> socket.socket:
        nonlocal connector_called
        connector_called = True
        raise AssertionError("private destination must not be connected")

    with validating_https_proxy(
        resolver=lambda _host, _port: ["127.0.0.1"],
        connector=connector,
        connect_timeout_seconds=1,
        maximum_elapsed_seconds=1,
    ) as proxy_url:
        proxy = urlsplit(proxy_url)
        with socket.create_connection((proxy.hostname, proxy.port), timeout=1) as client:
            client.sendall(b"CONNECT private.invalid:443 HTTP/1.1\r\nHost: private.invalid\r\n\r\n")
            response = client.recv(1024)
    assert response.startswith(b"HTTP/1.1 403")
    assert connector_called is False


def test_validating_proxy_rejects_plain_http_request() -> None:
    with validating_https_proxy(
        resolver=public_resolver,
        connect_timeout_seconds=1,
        maximum_elapsed_seconds=1,
    ) as proxy_url:
        proxy = urlsplit(proxy_url)
        with socket.create_connection((proxy.hostname, proxy.port), timeout=1) as client:
            client.sendall(
                b"GET http://media.example.invalid/video HTTP/1.1\r\n"
                b"Host: media.example.invalid\r\n\r\n"
            )
            response = client.recv(1024)
    assert response.startswith(b"HTTP/1.1 403")


def test_validating_proxy_pins_public_connect_destination() -> None:
    connected: list[tuple[str, int]] = []
    peer: socket.socket | None = None

    def connector(host: str, port: int, _timeout: float) -> socket.socket:
        nonlocal peer
        connected.append((host, port))
        upstream, peer = socket.socketpair()
        return upstream

    with validating_https_proxy(
        resolver=public_resolver,
        connector=connector,
        connect_timeout_seconds=1,
        maximum_elapsed_seconds=1,
    ) as proxy_url:
        proxy = urlsplit(proxy_url)
        with socket.create_connection((proxy.hostname, proxy.port), timeout=1) as client:
            client.sendall(b"CONNECT media.example.invalid:443 HTTP/1.1\r\n\r\n")
            response = client.recv(1024)
        if peer is not None:
            peer.close()
    assert response.startswith(b"HTTP/1.1 200")
    assert connected == [("93.184.216.34", 443)]
