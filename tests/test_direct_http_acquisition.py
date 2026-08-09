from __future__ import annotations

import gzip
import json
import os
import ssl
import stat
from pathlib import Path

import httpcore
import httpx
import pytest

from video_evidence_pipeline.acquisition.direct_http import DirectHttpAdapter
from video_evidence_pipeline.acquisition.http_transport import build_httpx_client
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
)
from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    initialize_job,
    job_status,
)
from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.domain.job import SourceKind, load_job_yaml
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.jsonl import read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace


def public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


class _StallingBodyStream(httpcore.NetworkStream):
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.read_timeouts: list[float | None] = []
        self.read_count = 0

    def read(self, _max_bytes: int, timeout: float | None = None) -> bytes:
        self.read_timeouts.append(timeout)
        self.read_count += 1
        if self.read_count == 1:
            self.clock[0] = 9.5
            return (
                b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n"
                b"Content-Type: video/mp4\r\nConnection: close\r\n\r\nfirst"
            )
        raise httpcore.ReadTimeout

    def write(self, _buffer: bytes, timeout: float | None = None) -> None:
        del timeout

    def close(self) -> None:
        pass

    def start_tls(
        self,
        _ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        del server_hostname, timeout
        return self


class _SingleStreamBackend(httpcore.NetworkBackend):
    def __init__(self, stream: httpcore.NetworkStream) -> None:
        self.stream = stream

    def connect_tcp(
        self,
        _host: str,
        _port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        del timeout, local_address, socket_options
        return self.stream


def network_job(tmp_path: Path, locator: str = "https://media.example.invalid/video.mp4") -> Path:
    workspace, _ = initialize_job(
        tmp_path,
        job_id="network-demo",
        source_kind=SourceKind.DIRECT_HTTP,
        source=locator,
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic network fixture",
        allow_local_processing=True,
        allow_network_acquisition=True,
        allow_external_api=False,
        confirmer_identity="tester",
    )
    return workspace.root


def adapter_for(
    handler,
    *,
    runtime: AcquisitionRuntimeConfig | None = None,
    resolver=public_resolver,
) -> DirectHttpAdapter:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return DirectHttpAdapter(
        runtime or AcquisitionRuntimeConfig(maximum_download_bytes=1024),
        client=client,
        resolver=resolver,
    )


def test_direct_http_streams_once_and_reuses_valid_network_bundle(tmp_path: Path) -> None:
    calls = 0
    payload = b"synthetic media bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4", "content-length": str(len(payload))},
            content=payload,
        )

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    first = acquire_job(job_path, adapter.runtime, adapter)
    status, exit_code = job_status(job_path)
    second = acquire_job(job_path, adapter.runtime, adapter)

    artifact = first.source_manifest.artifact_record.artifact
    assert artifact.content_sha256 == sha256_bytes(payload)
    assert artifact.size_bytes == len(payload)
    assert (job_path / artifact.relative_storage_path).read_bytes() == payload
    assert calls == 1
    assert second.reused is True
    assert len(read_jsonl(job_path / "manifests/transitions.jsonl", TransitionRecord)) == 3
    assert status["stored_state"] == "ACQUIRED"
    assert exit_code == 0


def test_network_source_manifest_crash_window_recovers_without_network(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"media")

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    job = load_job_yaml(job_path / "job.yaml")
    workspace = JobWorkspace.open(job_path)
    adapter.acquire(job, workspace)
    workspace.acquisition_manifest_json.unlink()

    result = acquire_job(job_path, adapter.runtime, adapter)

    assert calls == 1
    assert result.reused is True
    assert workspace.acquisition_manifest_json.is_file()
    status, exit_code = job_status(job_path)
    assert status["stored_state"] == "ACQUIRED"
    assert exit_code == 0


def test_direct_http_relative_redirect_is_resolved(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/media/final.mp4"})
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"media")

    job_path = network_job(tmp_path, "https://media.example.invalid/start")
    adapter = adapter_for(handler)
    result = acquire_job(job_path, adapter.runtime, adapter)
    assert seen == [
        "https://media.example.invalid/start",
        "https://media.example.invalid/media/final.mp4",
    ]
    assert result.source_manifest.acquisition_evidence.redirect_count == 1  # type: ignore[union-attr]


def test_direct_http_public_to_private_redirect_is_rejected(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://private.invalid/video"})

    def resolver(host: str, _port: int) -> list[str]:
        return ["127.0.0.1"] if host == "private.invalid" else ["93.184.216.34"]

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler, resolver=resolver)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, adapter.runtime, adapter)
    assert captured.value.code == "NETWORK_SSRF_REJECTED"


@pytest.mark.parametrize(
    ("location", "expected_code"),
    [
        ("http://media.example.invalid/video", "NETWORK_URL_REJECTED"),
        (
            "https://media.example.invalid/video?X-Amz-" + "Signature=synthetic-secret",
            "NETWORK_SIGNED_URL_REJECTED",
        ),
    ],
)
def test_direct_http_rejects_downgrade_and_signed_redirect(
    tmp_path: Path, location: str, expected_code: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": location})

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, adapter.runtime, adapter)
    assert captured.value.code == expected_code
    assert "synthetic-secret" not in captured.value.safe_message


def test_direct_http_redirect_loop_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        location = "/b" if request.url.path != "/b" else "/video.mp4"
        return httpx.Response(302, headers={"location": location})

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, adapter.runtime, adapter)
    assert captured.value.code == "HTTP_REDIRECT_LOOP"


def test_direct_http_redirect_limit_is_bounded(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"/next{len(request.url.path)}"})

    runtime = AcquisitionRuntimeConfig(maximum_download_bytes=1024, maximum_redirects=1)
    job_path = network_job(tmp_path)
    adapter = adapter_for(handler, runtime=runtime)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, runtime, adapter)
    assert captured.value.code == "HTTP_REDIRECT_LIMIT"


def test_direct_http_timeout_is_safely_mapped(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic token must not escape", request=request)

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, adapter.runtime, adapter)
    assert captured.value.code == "NETWORK_TIMEOUT"
    assert "token" not in captured.value.safe_message
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_direct_http_rejects_unsolicited_partial_content(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={
                "content-length": "5",
                "content-range": "bytes 0-4/10",
                "content-type": "video/mp4",
            },
            content=b"media",
        )

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, adapter.runtime, adapter)
    assert captured.value.code == "HTTP_ACQUISITION_FAILED"


def test_direct_http_rejects_content_range_even_with_status_200(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-length": "5",
                "content-range": "bytes 0-4/10",
                "content-type": "video/mp4",
            },
            content=b"media",
        )

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, adapter.runtime, adapter)
    assert captured.value.code == "HTTP_CONTENT_REJECTED"


def test_direct_http_total_download_deadline_is_bounded(tmp_path: Path) -> None:
    ticks = iter([0.0, 0.0, 2.0])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"media")

    runtime = AcquisitionRuntimeConfig(maximum_download_bytes=1024, maximum_download_seconds=1)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    adapter = DirectHttpAdapter(
        runtime,
        client=client,
        resolver=public_resolver,
        monotonic=lambda: next(ticks),
    )
    job_path = network_job(tmp_path)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, runtime, adapter)
    assert captured.value.code == "NETWORK_TIMEOUT"


def test_direct_http_stalled_next_read_uses_current_absolute_deadline(tmp_path: Path) -> None:
    clock = [0.0]
    stream = _StallingBodyStream(clock)
    runtime = AcquisitionRuntimeConfig(
        maximum_download_bytes=1024,
        maximum_download_seconds=10,
        network_timeout_seconds=30,
    )
    client_deadline = AcquisitionDeadline.start(10, monotonic=lambda: clock[0])
    job_path = network_job(tmp_path)

    with (
        build_httpx_client(
            runtime,
            resolver=public_resolver,
            deadline=client_deadline,
            network_backend=_SingleStreamBackend(stream),
        ) as client,
        pytest.raises(VepError) as captured,
    ):
        acquire_job(
            job_path,
            runtime,
            DirectHttpAdapter(
                runtime,
                client=client,
                resolver=public_resolver,
                monotonic=lambda: clock[0],
            ),
        )

    assert captured.value.code == "NETWORK_TIMEOUT"
    assert len(stream.read_timeouts) == 2
    assert stream.read_timeouts[0] == pytest.approx(10)
    assert stream.read_timeouts[1] is not None
    assert 0 < stream.read_timeouts[1] <= 0.5
    workspace = JobWorkspace.open(job_path)
    assert not workspace.source_manifest_json.exists()
    assert not workspace.acquisition_manifest_json.exists()
    assert list(workspace.safe_path("source/staging").iterdir()) == []
    status, _exit_code = job_status(job_path)
    assert status["stored_state"] == "AUTHORIZED"
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert all(record.to_state.value != "ACQUIRED" for record in transitions)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("headers", "body", "maximum_bytes", "expected_code"),
    [
        ({"content-length": "100"}, b"small", 8, "SOURCE_TOO_LARGE"),
        ({}, b"0123456789", 8, "SOURCE_TOO_LARGE"),
        ({}, b"", 1024, "SOURCE_EMPTY"),
        (
            {"content-type": "text/html"},
            b"<html>error</html>",
            1024,
            "HTTP_CONTENT_REJECTED",
        ),
        (
            {"content-type": "application/json"},
            b'{"error":true}',
            1024,
            "HTTP_CONTENT_REJECTED",
        ),
        ({}, b"<?xml version='1.0'?><error/>", 1024, "HTTP_CONTENT_REJECTED"),
        ({"content-encoding": "gzip"}, gzip.compress(b"encoded"), 1024, "HTTP_CONTENT_REJECTED"),
        ({"content-length": "99"}, b"short", 1024, "HTTP_CONTENT_LENGTH_MISMATCH"),
    ],
)
def test_direct_http_content_and_size_validation(
    tmp_path: Path,
    headers: dict[str, str],
    body: bytes,
    maximum_bytes: int,
    expected_code: str,
) -> None:
    runtime = AcquisitionRuntimeConfig(maximum_download_bytes=maximum_bytes)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=body)

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler, runtime=runtime)
    with pytest.raises(VepError) as captured:
        acquire_job(job_path, runtime, adapter)
    assert captured.value.code == expected_code
    assert not (job_path / "manifests/source-manifest.json").exists()


def test_direct_http_machine_artifacts_do_not_persist_url_or_query(tmp_path: Path) -> None:
    locator = "https://media.example.invalid/video.mp4?course=synthetic"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"media")

    job_path = network_job(tmp_path, locator)
    adapter = adapter_for(handler)
    result = acquire_job(job_path, adapter.runtime, adapter)
    manifest_paths = [
        job_path / "manifests/source-manifest.json",
        job_path / "manifests/stages/acquisition.json",
        job_path / "manifests/transitions.jsonl",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in manifest_paths)
    assert locator not in combined
    assert "course=synthetic" not in combined
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.requested_host == "media.example.invalid"
    assert evidence.final_host == "media.example.invalid"
    assert evidence.credential_mode == "none"
    assert evidence.actual_acquired_size == 5
    assert json.loads(manifest_paths[0].read_text(encoding="utf-8"))["source_kind"] == (
        "direct_http"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not a Windows guarantee")
def test_network_staging_publish_preserves_private_posix_modes(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"media")

    job_path = network_job(tmp_path)
    adapter = adapter_for(handler)
    result = acquire_job(job_path, adapter.runtime, adapter)
    artifact = result.source_manifest.artifact_record.artifact
    raw = job_path / artifact.relative_storage_path
    directories = [job_path / "source/staging", raw.parent]
    files = [
        raw,
        job_path / "manifests/source-manifest.json",
        job_path / "manifests/stages/acquisition.json",
    ]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
