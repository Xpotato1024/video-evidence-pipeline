from __future__ import annotations

import importlib.metadata
import ssl
import time
from pathlib import Path

import httpcore
import pytest
from pydantic import ValidationError

from video_evidence_pipeline.acquisition.http_transport import (
    _PinnedResolverBackend,
    build_httpx_client,
)
from video_evidence_pipeline.acquisition.network_policy import validate_network_url
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
)
from video_evidence_pipeline.application import authorize_job, initialize_job
from video_evidence_pipeline.domain.authorization import AuthorizationRecord
from video_evidence_pipeline.domain.job import JobSpec, SourceKind
from video_evidence_pipeline.domain.locators import (
    canonicalize_box_locator,
    canonicalize_https_locator,
    canonicalize_panopto_locator,
    has_sensitive_query,
)
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.atomic import read_json_model
from video_evidence_pipeline.storage.layout import JobWorkspace


def public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


class _StaticHttpStream(httpcore.NetworkStream):
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.writes: list[bytes] = []
        self.tls_server_names: list[str | None] = []

    def read(self, _max_bytes: int, timeout: float | None = None) -> bytes:
        del timeout
        response, self.response = self.response, b""
        return response

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        self.writes.append(buffer)

    def close(self) -> None:
        pass

    def start_tls(
        self,
        _ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        del timeout
        self.tls_server_names.append(server_hostname)
        return self


class _RecordingBackend(httpcore.NetworkBackend):
    def __init__(self, stream: httpcore.NetworkStream) -> None:
        self.stream = stream
        self.connect_hosts: list[str] = []

    def connect_tcp(
        self,
        host: str,
        _port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        del timeout, local_address, socket_options
        self.connect_hosts.append(host)
        return self.stream


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ("https://EXAMPLE.invalid/video", "https://example.invalid/video"),
        (
            "https://www.youtube.com/watch?v=abc123&list=PL1",
            "https://www.youtube.com/watch?v=abc123&list=PL1",
        ),
    ],
)
def test_stable_https_locator_accepts_https_and_benign_query(locator: str, expected: str) -> None:
    assert canonicalize_https_locator(locator) == expected


@pytest.mark.parametrize(
    "locator",
    [
        "http://example.invalid/video",
        "https://user:password@example.invalid/video",
        "https://example.invalid/video#fragment",
        "https://example.invalid/video?token=secret",
        "https://example.invalid/video?access_token=secret",
        "https://example.invalid/video?X-Amz-Signature=secret",
        "https://example.invalid/video?X-Goog-Credential=secret",
        "https://example.invalid/video?Policy=secret&Signature=value&Key-Pair-Id=key",
        "https://example.invalid/video?jwt=secret",
        "https://example.invalid/video?api_key=secret",
        "https://example.invalid/video?hdnea=exp~acl~hmac",
        "https://example.invalid/video?client_secret=secret",
        "https://example.invalid/video?token=",
        "https://example.invalid/video?AWSAccessKeyId=secret",
        "https://example.invalid/video?h=opaque&t=123",
        "https://example.invalid/video?opaque=credential-shaped-value",
    ],
)
def test_unstable_or_secret_https_locator_is_rejected(locator: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_https_locator(locator)


@pytest.mark.parametrize(
    "locator",
    [
        "https://example.invalid/video?auth=x",
        "https://example.invalid/video?authorization=x",
        "https://example.invalid/video?sig=x",
        "https://example.invalid/video?expires=1",
    ],
)
def test_sensitive_query_detection_is_shared(locator: str) -> None:
    assert has_sensitive_query(locator)


def test_panopto_viewer_url_is_reduced_to_tenant_session_identity() -> None:
    locator = (
        "https://TENANT.hosted.panopto.com/Panopto/Pages/Viewer.aspx?"
        "id=12345678-1234-1234-1234-123456789abc"
    )
    assert canonicalize_panopto_locator(locator) == (
        "panopto://tenant.hosted.panopto.com/session/12345678-1234-1234-1234-123456789abc"
    )


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ("12345", "box://file/12345"),
        ("https://acme.app.box.com/files/12345", "box://file/12345"),
        ("box://file/12345", "box://file/12345"),
    ],
)
def test_box_file_identity_is_canonical(locator: str, expected: str) -> None:
    assert canonicalize_box_locator(locator) == expected


def test_box_shared_link_is_not_a_source_of_truth() -> None:
    with pytest.raises(ValueError):
        canonicalize_box_locator("https://app.box.com/s/shared-secret")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "0.0.0.0",
        "192.0.2.1",
    ],
)
def test_ssrf_policy_rejects_non_global_ipv4_and_ipv6(address: str) -> None:
    with pytest.raises(VepError) as captured:
        validate_network_url(
            "https://example.invalid/video",
            resolver=lambda _host, _port: [address],
        )
    assert captured.value.code == "NETWORK_SSRF_REJECTED"


def test_ssrf_policy_accepts_public_https() -> None:
    assert validate_network_url("https://example.invalid/video", resolver=public_resolver) == (
        "example.invalid"
    )


def test_dns_resolution_timeout_is_safely_mapped() -> None:
    def slow_resolver(_host: str, _port: int) -> list[str]:
        time.sleep(0.1)
        return ["93.184.216.34"]

    with pytest.raises(VepError) as captured:
        validate_network_url(
            "https://example.invalid/video",
            resolver=slow_resolver,
            resolve_timeout_seconds=0.01,
        )
    assert captured.value.code == "NETWORK_TIMEOUT"
    assert captured.value.__cause__ is None


def test_ssrf_policy_rejects_localhost_without_dns() -> None:
    with pytest.raises(VepError) as captured:
        validate_network_url("https://localhost/video", resolver=public_resolver)
    assert captured.value.code == "NETWORK_SSRF_REJECTED"


def test_connection_backend_revalidates_and_pins_the_resolved_address() -> None:
    connected: list[str] = []

    class FakeBackend:
        def connect_tcp(self, host: str, _port: int, **_kwargs):
            connected.append(host)
            return object()

    backend = _PinnedResolverBackend(
        lambda _host, _port: ["93.184.216.34"],
        AcquisitionRuntimeConfig(),
        None,
        backend=FakeBackend(),  # type: ignore[arg-type]
    )
    backend.connect_tcp("example.invalid", 443)
    assert connected == ["93.184.216.34"]


def test_connection_backend_rejects_dns_rebinding_before_connect() -> None:
    backend = _PinnedResolverBackend(
        lambda _host, _port: ["127.0.0.1"],
        AcquisitionRuntimeConfig(),
        None,
    )
    with pytest.raises(VepError) as captured:
        backend.connect_tcp("example.invalid", 443)
    assert captured.value.code == "NETWORK_SSRF_REJECTED"


def test_production_httpx_transport_uses_pinned_public_ip_and_custom_pool() -> None:
    stream = _StaticHttpStream(
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nmedia"
    )
    backend = _RecordingBackend(stream)
    runtime = AcquisitionRuntimeConfig(network_timeout_seconds=30)
    deadline = AcquisitionDeadline.start(10, monotonic=lambda: 0.0)
    with build_httpx_client(
        runtime,
        resolver=lambda host, _port: ["93.184.216.34"] if host == "media.example.invalid" else [],
        deadline=deadline,
        network_backend=backend,
    ) as client:
        response = client.get("https://media.example.invalid/video.mp4")

    assert response.content == b"media"
    assert backend.connect_hosts == ["93.184.216.34"]
    assert "media.example.invalid" not in backend.connect_hosts
    assert stream.tls_server_names == ["media.example.invalid"]
    assert any(b"GET /video.mp4 HTTP/1.1" in write for write in stream.writes)


def test_production_httpx_transport_rejects_rebound_ip_before_socket_connect() -> None:
    stream = _StaticHttpStream(b"")
    backend = _RecordingBackend(stream)
    with (
        build_httpx_client(
            AcquisitionRuntimeConfig(),
            resolver=lambda _host, _port: ["127.0.0.1"],
            network_backend=backend,
        ) as client,
        pytest.raises(VepError) as captured,
    ):
        client.get("https://media.example.invalid/video.mp4")

    assert captured.value.code == "NETWORK_SSRF_REJECTED"
    assert backend.connect_hosts == []


def test_httpx_httpcore_security_pair_is_exactly_audited_versions() -> None:
    assert importlib.metadata.version("httpx") == "0.28.1"
    assert importlib.metadata.version("httpcore") == "1.0.9"


def _job(root: Path, kind: SourceKind, locator: str) -> Path:
    workspace, _ = initialize_job(
        root,
        job_id="network-job",
        source_kind=kind,
        source=locator,
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    return workspace.root


def test_network_acquisition_is_denied_by_default(tmp_path: Path) -> None:
    path = _job(tmp_path, SourceKind.DIRECT_HTTP, "https://example.invalid/video")
    record = authorize_job(
        path,
        confirm=True,
        basis="authorized synthetic source",
        allow_local_processing=True,
        allow_external_api=True,
        confirmer_identity="tester",
    )
    assert record.allow_network_acquisition is False


def test_old_authorization_json_defaults_network_permission_to_deny(tmp_path: Path) -> None:
    path = _job(tmp_path, SourceKind.DIRECT_HTTP, "https://example.invalid/video")
    workspace = JobWorkspace.open(path)
    authorization = {
        "schema_version": "1.0",
        "job_id": "network-job",
        "job_fingerprint": "a" * 64,
        "confirmed_by_user": True,
        "authorization_basis": "legacy record",
        "allow_local_processing": True,
        "allow_external_api": False,
        "confirmed_at": "2026-08-08T00:00:00Z",
        "confirmer_identity": "tester",
    }
    workspace.authorization_json.write_text(
        __import__("json").dumps(authorization), encoding="utf-8"
    )
    record = read_json_model(
        workspace.authorization_json,
        AuthorizationRecord,
        code="AUTHORIZATION_INVALID",
    )
    assert record.allow_network_acquisition is False


def test_source_spec_requires_precanonicalized_panopto_locator() -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate(
            {
                "schema_version": "1.0",
                "job_id": "demo",
                "created_at": "2026-08-08T00:00:00Z",
                "source": {
                    "kind": "panopto",
                    "locator": "https://tenant.invalid/Panopto/Pages/Viewer.aspx?id="
                    "12345678-1234-1234-1234-123456789abc",
                },
                "assignment": {"task_type": "report", "language": "ja", "requirements": []},
                "output": {"format": "markdown"},
            }
        )
