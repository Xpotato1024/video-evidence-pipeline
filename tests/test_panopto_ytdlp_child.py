from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_ytdlp_acquisition import required_ffmpeg_bin
from yt_dlp import YoutubeDL
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, ExtractorError

from video_evidence_pipeline.acquisition import panopto_ytdlp_child as child
from video_evidence_pipeline.acquisition.panopto_ytdlp_protocol import (
    PANOPTO_CHILD_EXIT_REAUTH_REQUIRED,
    PANOPTO_CHILD_EXIT_VIEWER_DENIED,
)
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionRuntimeConfig,
    BrowserCookieSource,
)
from video_evidence_pipeline.acquisition.ytdlp_runner import run_ytdlp
from video_evidence_pipeline.application import initialize_job
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.media import ToolIdentity
from video_evidence_pipeline.errors import VepError

SESSION_ID = "12345678-1234-1234-1234-123456789abc"
TENANT = "tenant.hosted.panopto.com"
VIEWER_URL = f"https://{TENANT}/Panopto/Pages/Viewer.aspx?id={SESSION_ID}"


def test_pinned_player_saves_complete_progressive_file(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [
            str(required_ffmpeg_bin() / "ffmpeg"),
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=3",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    media = source.read_bytes()
    media_url = "https://cdn.example/authorized.mp4"
    workspace, _ = initialize_job(
        tmp_path,
        job_id="lecture",
        source_kind=SourceKind.PANOPTO,
        source=VIEWER_URL,
        task_type="summary",
        language="ja",
        output_format="markdown",
    )
    calls = []

    def urlopen(_downloader, request):
        url = request.url
        calls.append(url)
        if url == media_url:
            body, content_type = media, "video/mp4"
        else:
            parsed = urlsplit(url)
            assert parsed.hostname == TENANT and parsed.path.endswith("/Viewer/DeliveryInfo.aspx")
            assert parse_qs(parsed.query)["deliveryId"] == [SESSION_ID]
            body = json.dumps(
                {
                    "Delivery": {
                        "SessionName": "lecture",
                        "Duration": 3,
                        "Streams": [{"StreamHttpUrl": media_url, "ViewerMediaFileTypeName": "mp4"}],
                    }
                }
            ).encode()
            content_type = "application/json"
        return Response(
            io.BytesIO(body), url, {"Content-Type": content_type, "Content-Length": str(len(body))}
        )

    monkeypatch.setattr(YoutubeDL, "urlopen", urlopen)

    def in_process_child(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, child.main(argv[3:]))

    staged = run_ytdlp(
        VIEWER_URL,
        workspace,
        AcquisitionRuntimeConfig(),
        adapter_kind=SourceKind.PANOPTO,
        runner=in_process_child,
        resolver=lambda *_: ["93.184.216.34"],
    )
    assert staged.path.read_bytes() == media
    assert staged.evidence.acquisition_mode.value == "progressive"
    assert any(url == media_url for url in calls)


class SyntheticToolchain:
    def __init__(self, root: Path) -> None:
        self.ffmpeg_location = root.resolve()
        self.ffmpeg_identity = ToolIdentity(
            name="ffmpeg",
            version="synthetic",
            executable_sha256="1" * 64,
            capabilities=[],
        )

    def validate_final(self, _path: Path, _deadline: object) -> int:
        return 60_000

    def revalidate(self) -> None:
        return None


def _panopto_download_error(
    status: int,
    *,
    host: str = TENANT,
    path: str = "/Panopto/Pages/Viewer/DeliveryInfo.aspx",
) -> DownloadError:
    response = Response(
        fp=io.BytesIO(),
        url=f"https://{host}{path}?deliveryId={SESSION_ID}&secret=not-persisted",
        headers={},
        status=status,
        reason="Forbidden" if status == 403 else "Unauthorized",
    )
    http_error = HTTPError(response)
    extraction_error = ExtractorError("provider error is suppressed", cause=http_error)
    return DownloadError(
        "raw yt-dlp error is suppressed",
        exc_info=(ExtractorError, extraction_error, None),
    )


def test_pinned_panopto_error_code_2_is_a_typed_reauthentication_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def stub_urlopen(request: object) -> Response:
        url = request.url  # type: ignore[attr-defined]
        calls.append(url)
        return Response(
            fp=io.BytesIO(b'{"ErrorCode":2}'),
            url=url,
            headers={"Content-Type": "application/json"},
        )

    with YoutubeDL({"quiet": True, "no_warnings": True, "cachedir": False}) as downloader:
        extractor = child._VepPanoptoIE(downloader)
        downloader.add_info_extractor(extractor)
        monkeypatch.setattr(downloader, "urlopen", stub_urlopen)

        with pytest.raises(child.PanoptoReauthenticationRequired):
            downloader.extract_info(
                VIEWER_URL,
                download=True,
                ie_key=child._VepPanoptoIE.ie_key(),
            )
        assert downloader.get_info_extractor("Panopto") is extractor

    assert len(calls) == 1
    request_url = urlsplit(calls[0])
    assert request_url.scheme == "https"
    assert request_url.hostname == TENANT
    assert request_url.path == "/Panopto/Pages/Viewer/DeliveryInfo.aspx"
    assert parse_qs(request_url.query)["deliveryId"] == [SESSION_ID]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, PANOPTO_CHILD_EXIT_REAUTH_REQUIRED),
        (403, PANOPTO_CHILD_EXIT_VIEWER_DENIED),
    ],
)
def test_only_tenant_delivery_info_http_status_is_classified(status: int, expected: int) -> None:
    assert child.classify_download_error(_panopto_download_error(status), TENANT) == expected
    assert (
        child.classify_download_error(
            _panopto_download_error(status, host="cdn.panopto.example"), TENANT
        )
        is None
    )
    assert (
        child.classify_download_error(
            _panopto_download_error(status, path="/Panopto/api/v1/sessions/" + SESSION_ID),
            TENANT,
        )
        is None
    )


def test_unclassified_extractor_error_stays_generic_even_with_login_wording() -> None:
    error = DownloadError(
        "This video is only available for registered users",
        exc_info=(
            ExtractorError,
            ExtractorError("This video is only available for registered users"),
            None,
        ),
    )

    assert child.classify_download_error(error, TENANT) is None


class RecordingYoutubeDL:
    def __init__(self, options: dict[str, Any]) -> None:
        self.params = options
        self._download_retcode = 0
        self.extract_calls: list[tuple[str, dict[str, object]]] = []
        self.extractors: dict[str, object] = {}

    def __enter__(self) -> RecordingYoutubeDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def add_info_extractor(self, extractor: object) -> None:
        self.extractors[extractor.ie_key()] = extractor  # type: ignore[attr-defined]

    def extract_info(self, url: str, **kwargs: object) -> dict[str, str]:
        self.extract_calls.append((url, kwargs))
        return {"id": SESSION_ID}


def test_child_uses_only_panopto_ie_and_disables_side_effecting_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[RecordingYoutubeDL] = []

    def factory(options: dict[str, Any]) -> RecordingYoutubeDL:
        downloader = RecordingYoutubeDL(options)
        captured.append(downloader)
        return downloader

    monkeypatch.setattr(child, "YoutubeDL", factory)
    argv = [
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-js-runtimes",
        "--no-update",
        "--no-playlist",
        "--no-mark-watched",
        "--no-check-formats",
        "--",
        VIEWER_URL,
    ]

    assert child.main(argv) == 0
    downloader = captured[0]
    assert downloader.params["mark_watched"] is False
    assert downloader.params["ignoreerrors"] is False
    assert isinstance(downloader.extractors["Panopto"], child._VepPanoptoIE)
    assert downloader.extract_calls == [(VIEWER_URL, {"download": True, "ie_key": "Panopto"})]


def test_child_rejects_non_panopto_or_multiple_urls_before_downloader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_factory(_options: dict[str, Any]) -> RecordingYoutubeDL:
        raise AssertionError("invalid input must not construct yt-dlp")

    monkeypatch.setattr(child, "YoutubeDL", unexpected_factory)
    assert child.main(["--", "https://example.com/video"]) == 1
    assert child.main(["--", VIEWER_URL, VIEWER_URL]) == 1


@pytest.mark.parametrize(
    ("returncode", "expected_code"),
    [
        (PANOPTO_CHILD_EXIT_REAUTH_REQUIRED, "PANOPTO_REAUTH_REQUIRED"),
        (PANOPTO_CHILD_EXIT_VIEWER_DENIED, "PANOPTO_VIEWER_DENIED"),
        (1, "YTDLP_ACQUISITION_FAILED"),
    ],
)
def test_panopto_runner_maps_only_reserved_typed_child_codes(
    tmp_path: Path,
    returncode: int,
    expected_code: str,
) -> None:
    workspace, _ = initialize_job(
        tmp_path,
        job_id=f"panopto-child-{returncode}",
        source_kind=SourceKind.PANOPTO,
        source=VIEWER_URL,
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    runtime = AcquisitionRuntimeConfig(
        cookies_from_browser=BrowserCookieSource.FIREFOX,
        panopto_ytdlp_fallback=True,
        maximum_download_bytes=1024,
    )
    toolchain = SyntheticToolchain(tmp_path)
    captured: dict[str, object] = {}

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode)

    with pytest.raises(VepError) as error:
        run_ytdlp(
            VIEWER_URL,
            workspace,
            runtime,
            adapter_kind=SourceKind.PANOPTO,
            runner=runner,
            resolver=lambda _host, _port: ["93.184.216.34"],
            toolchain_factory=lambda _deadline: toolchain,
        )

    assert error.value.code == expected_code
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[1:3] == ["-m", "video_evidence_pipeline.acquisition.panopto_ytdlp_child"]
    assert "--no-mark-watched" in argv
    assert "--no-check-formats" not in argv
    staging = workspace.safe_path("source/staging")
    assert not list(staging.iterdir())


def test_generic_runner_does_not_interpret_panopto_reserved_exit_codes(
    tmp_path: Path,
) -> None:
    workspace, _ = initialize_job(
        tmp_path,
        job_id="generic-reserved-exit",
        source_kind=SourceKind.GENERIC_YTDLP,
        source="https://video.example/watch?id=abc123",
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    runtime = AcquisitionRuntimeConfig(maximum_download_bytes=1024)
    toolchain = SyntheticToolchain(tmp_path)

    with pytest.raises(VepError) as error:
        run_ytdlp(
            "https://video.example/watch?id=abc123",
            workspace,
            runtime,
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=lambda argv, **_kwargs: subprocess.CompletedProcess(
                argv, PANOPTO_CHILD_EXIT_REAUTH_REQUIRED
            ),
            resolver=lambda _host, _port: ["93.184.216.34"],
            toolchain_factory=lambda _deadline: toolchain,
        )

    assert error.value.code == "YTDLP_ACQUISITION_FAILED"
