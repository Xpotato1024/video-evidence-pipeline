from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from video_evidence_pipeline.acquisition.generic_ytdlp import GenericYtdlpAdapter
from video_evidence_pipeline.acquisition.https_proxy import validating_https_proxy
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionRuntimeConfig,
    BrowserCookieSource,
)
from video_evidence_pipeline.acquisition.ytdlp_runner import hardened_argv, run_ytdlp
from video_evidence_pipeline.application import acquire_job, authorize_job, initialize_job
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.layout import JobWorkspace


def public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


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
        proxy_url="http://127.0.0.1:43210",
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
        "--proxy",
        "http://127.0.0.1:43210",
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
    plain = hardened_argv(
        "https://example.invalid/watch",
        tmp_path,
        AcquisitionRuntimeConfig(),
        proxy_url="http://127.0.0.1:43210",
    )
    cookie = hardened_argv(
        "https://example.invalid/watch",
        tmp_path,
        AcquisitionRuntimeConfig(cookies_from_browser=BrowserCookieSource.FIREFOX),
        proxy_url="http://127.0.0.1:43210",
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
        resolver=public_resolver,
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["cwd"] == staged.cleanup_root
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["YTDLP_NO_PLUGINS"] == "1"
    assert "VEP_BOX_ACCESS_TOKEN" not in environment
    assert staged.path.name == "source.mp4"
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--proxy") + 1].startswith("http://127.0.0.1:")


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
        )
    assert error.value.code == "SOURCE_TOO_LARGE"
    assert process.terminated is True
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


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
            resolver=public_resolver,
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
    adapter = GenericYtdlpAdapter(
        runtime,
        runner=successful_runner(captured),
        resolver=public_resolver,
    )
    result = acquire_job(workspace.root, runtime, adapter)
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.backend == "yt-dlp"
    assert evidence.credential_mode == "browser-cookie"
    assert evidence.http_status_category == "not-observed"
    assert evidence.final_host is None
    assert evidence.redirect_count is None
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
