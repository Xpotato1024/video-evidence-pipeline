from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from video_evidence_pipeline.acquisition.box import BoxAdapter
from video_evidence_pipeline.acquisition.panopto import PanoptoAdapter
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionRuntimeConfig,
    BrowserCookieSource,
)
from video_evidence_pipeline.application import acquire_job, authorize_job, initialize_job
from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.domain.job import SourceKind, load_job_yaml
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.layout import JobWorkspace

SESSION_ID = "12345678-1234-1234-1234-123456789abc"
PANOPTO_VIEWER = "https://tenant.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=" + SESSION_ID
SIGNED_SECRET = "synthetic-signed-secret"
TOKEN_SECRET = "synthetic-token-secret"


def public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def initialized_authorized_job(tmp_path: Path, kind: SourceKind, locator: str) -> JobWorkspace:
    workspace, _ = initialize_job(
        tmp_path,
        job_id=f"{kind.value.replace('_', '-')}-demo",
        source_kind=kind,
        source=locator,
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic provider fixture",
        allow_local_processing=True,
        allow_network_acquisition=True,
        allow_external_api=False,
        confirmer_identity="tester",
    )
    return workspace


def persisted_machine_text(workspace: JobWorkspace) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            workspace.source_manifest_json,
            workspace.acquisition_manifest_json,
            workspace.transitions_jsonl,
        ]
    )


def test_panopto_official(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"panopto synthetic media"
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), request.headers.get("authorization")))
        if request.url.host == "tenant.hosted.panopto.com":
            return httpx.Response(
                200,
                json={
                    "Id": SESSION_ID,
                    "Name": "Authorized Session.mp4",
                    "Urls": {
                        "DownloadUrl": (
                            "https://cdn.panopto.example/media.mp4?X-Amz-Signature=" + SIGNED_SECRET
                        )
                    },
                },
            )
        assert request.url.host == "cdn.panopto.example"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4", "content-length": str(len(payload))},
            content=payload,
        )

    monkeypatch.setenv("VEP_PANOPTO_ACCESS_TOKEN", TOKEN_SECRET)
    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    job = load_job_yaml(workspace.job_yaml)
    assert job.source.locator == ("panopto://tenant.hosted.panopto.com/session/" + SESSION_ID)
    runtime = AcquisitionRuntimeConfig(
        panopto_token_env="VEP_PANOPTO_ACCESS_TOKEN",
        panopto_tenant_host="tenant.hosted.panopto.com",
        maximum_download_bytes=1024,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    adapter = PanoptoAdapter(runtime, client=client, resolver=public_resolver)
    result = acquire_job(workspace.root, runtime, adapter)

    assert requests[0][0].endswith("/Panopto/api/v1/sessions/" + SESSION_ID)
    assert requests[0][1] == "Bearer " + TOKEN_SECRET
    assert requests[1][1] is None
    artifact = result.source_manifest.artifact_record.artifact
    assert artifact.content_sha256 == sha256_bytes(payload)
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.backend == "panopto-rest"
    assert evidence.credential_mode == "environment-token"
    assert evidence.requested_host == "tenant.hosted.panopto.com"
    assert evidence.final_host == "cdn.panopto.example"
    persisted = persisted_machine_text(workspace)
    assert SIGNED_SECRET not in persisted
    assert TOKEN_SECRET not in persisted
    assert "DownloadUrl" not in persisted


def test_panopto_download_unavailable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEP_PANOPTO_ACCESS_TOKEN", TOKEN_SECRET)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Id": SESSION_ID, "Urls": {"DownloadUrl": ""}})

    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig(
        panopto_token_env="VEP_PANOPTO_ACCESS_TOKEN",
        panopto_tenant_host="tenant.hosted.panopto.com",
    )
    adapter = PanoptoAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, adapter)
    assert captured.value.code == "PANOPTO_DOWNLOAD_UNAVAILABLE"


def test_panopto_download_policy_is_not_bypassed_by_explicit_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEP_PANOPTO_ACCESS_TOKEN", TOKEN_SECRET)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Id": SESSION_ID, "Urls": {"DownloadUrl": ""}})

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("provider download prohibition must not fall back")

    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig(
        panopto_token_env="VEP_PANOPTO_ACCESS_TOKEN",
        panopto_tenant_host="tenant.hosted.panopto.com",
        cookies_from_browser=BrowserCookieSource.FIREFOX,
        panopto_ytdlp_fallback=True,
    )
    adapter = PanoptoAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
        runner=runner,
    )
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, adapter)
    assert captured.value.code == "PANOPTO_DOWNLOAD_UNAVAILABLE"


def test_panopto_requires_exact_session_id_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEP_PANOPTO_ACCESS_TOKEN", TOKEN_SECRET)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Urls": {"DownloadUrl": "https://cdn.example.invalid/media"}},
        )

    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig(
        panopto_token_env="VEP_PANOPTO_ACCESS_TOKEN",
        panopto_tenant_host="tenant.hosted.panopto.com",
    )
    adapter = PanoptoAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, adapter)
    assert captured.value.code == "PANOPTO_API_INVALID"


def test_panopto_token_is_bound_to_explicit_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEP_PANOPTO_ACCESS_TOKEN", TOKEN_SECRET)
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig(
        panopto_token_env="VEP_PANOPTO_ACCESS_TOKEN",
        panopto_tenant_host="other.hosted.panopto.com",
    )
    adapter = PanoptoAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, adapter)
    assert captured.value.code == "PANOPTO_TENANT_MISMATCH"
    assert requests == 0


def test_provider_api_response_size_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEP_PANOPTO_ACCESS_TOKEN", TOKEN_SECRET)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b"x" * (1024 * 1024))

    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig(
        panopto_token_env="VEP_PANOPTO_ACCESS_TOKEN",
        panopto_tenant_host="tenant.hosted.panopto.com",
    )
    adapter = PanoptoAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, adapter)
    assert captured.value.code == "API_RESPONSE_INVALID"


def test_panopto_browser_cookie_fallback_requires_explicit_flag(tmp_path: Path) -> None:
    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig(cookies_from_browser=BrowserCookieSource.FIREFOX)
    adapter = PanoptoAdapter(runtime, resolver=public_resolver)
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, adapter)
    assert captured.value.code == "ACQUISITION_RUNTIME_CONFIG_INVALID"


def test_panopto_explicit_browser_cookie_fallback_uses_hardened_runner(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        directory = Path(argv[argv.index("--paths") + 1])
        (directory / "source.mp4").write_bytes(b"fallback media")
        return subprocess.CompletedProcess(argv, 0)

    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig(
        cookies_from_browser=BrowserCookieSource.FIREFOX,
        panopto_ytdlp_fallback=True,
        maximum_download_bytes=1024,
    )
    adapter = PanoptoAdapter(runtime, resolver=public_resolver, runner=runner)
    result = acquire_job(workspace.root, runtime, adapter)
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "--cookies-from-browser" in argv
    assert argv[-1].startswith("https://tenant.hosted.panopto.com/Panopto/Pages/Viewer.aspx")
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.backend == "yt-dlp"
    assert evidence.credential_mode == "browser-cookie"


def test_panopto_no_credential_and_access_denial_remain_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialized_authorized_job(tmp_path, SourceKind.PANOPTO, PANOPTO_VIEWER)
    runtime = AcquisitionRuntimeConfig()
    with pytest.raises(VepError) as missing:
        acquire_job(workspace.root, runtime, PanoptoAdapter(runtime, resolver=public_resolver))
    assert missing.value.code == "PANOPTO_CREDENTIAL_REQUIRED"

    monkeypatch.setenv("VEP_PANOPTO_ACCESS_TOKEN", TOKEN_SECRET)
    denied_runtime = AcquisitionRuntimeConfig(
        panopto_token_env="VEP_PANOPTO_ACCESS_TOKEN",
        panopto_tenant_host="tenant.hosted.panopto.com",
        cookies_from_browser=BrowserCookieSource.FIREFOX,
        panopto_ytdlp_fallback=True,
    )
    denied = PanoptoAdapter(
        denied_runtime,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(403))),
        resolver=public_resolver,
    )
    with pytest.raises(VepError) as access:
        acquire_job(workspace.root, denied_runtime, denied)
    assert access.value.code == "PANOPTO_ACCESS_DENIED"


def test_box_metadata_content_redirect_and_bearer_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"box synthetic media"
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/2.0/files/12345":
            return httpx.Response(
                200,
                json={"id": "12345", "type": "file", "name": "Box Video.mp4", "size": len(payload)},
            )
        if request.url.path == "/2.0/files/12345/content":
            return httpx.Response(
                302,
                headers={
                    "location": "https://dl.boxcloud.com/d/file.mp4?Signature=" + SIGNED_SECRET
                },
            )
        assert request.url.host == "dl.boxcloud.com"
        assert "authorization" not in request.headers
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=payload)

    monkeypatch.setenv("VEP_BOX_ACCESS_TOKEN", TOKEN_SECRET)
    workspace = initialized_authorized_job(
        tmp_path, SourceKind.BOX, "https://acme.app.box.com/files/12345"
    )
    assert load_job_yaml(workspace.job_yaml).source.locator == "box://file/12345"
    runtime = AcquisitionRuntimeConfig(
        box_token_env="VEP_BOX_ACCESS_TOKEN", maximum_download_bytes=1024
    )
    adapter = BoxAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    result = acquire_job(workspace.root, runtime, adapter)
    assert requests[0][1] == "Bearer " + TOKEN_SECRET
    assert requests[1][1] == "Bearer " + TOKEN_SECRET
    assert requests[2][1] is None
    artifact = result.source_manifest.artifact_record.artifact
    assert artifact.content_sha256 == sha256_bytes(payload)
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.backend == "box-rest"
    assert evidence.redirect_count == 1
    assert evidence.declared_content_length == len(payload)
    persisted = persisted_machine_text(workspace)
    assert SIGNED_SECRET not in persisted
    assert TOKEN_SECRET not in persisted
    assert "dl.boxcloud.com/d/" not in persisted


@pytest.mark.parametrize(
    ("status", "code"), [(401, "BOX_ACCESS_DENIED"), (403, "BOX_ACCESS_DENIED")]
)
def test_box_permission_denial_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    monkeypatch.setenv("VEP_BOX_ACCESS_TOKEN", TOKEN_SECRET)
    workspace = initialized_authorized_job(tmp_path, SourceKind.BOX, "12345")
    runtime = AcquisitionRuntimeConfig(box_token_env="VEP_BOX_ACCESS_TOKEN")
    adapter = BoxAdapter(
        runtime,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status, json={"token": TOKEN_SECRET})
            )
        ),
        resolver=public_resolver,
    )
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, adapter)
    assert captured.value.code == code
    assert TOKEN_SECRET not in captured.value.safe_message


def test_box_missing_token_is_structured(tmp_path: Path) -> None:
    workspace = initialized_authorized_job(tmp_path, SourceKind.BOX, "12345")
    runtime = AcquisitionRuntimeConfig(box_token_env="VEP_BOX_ACCESS_TOKEN_DOES_NOT_EXIST")
    with pytest.raises(VepError) as captured:
        acquire_job(workspace.root, runtime, BoxAdapter(runtime, resolver=public_resolver))
    assert captured.value.code == "BOX_CREDENTIAL_REQUIRED"


def test_box_retry_after_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"retry media"
    content_calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal content_calls
        if request.url.path == "/2.0/files/12345":
            return httpx.Response(
                200,
                json={"id": "12345", "type": "file", "name": "retry.mp4", "size": len(payload)},
            )
        if request.url.path == "/2.0/files/12345/content":
            content_calls += 1
            if content_calls == 1:
                return httpx.Response(202, headers={"retry-after": "1"})
            return httpx.Response(200, content=payload)
        raise AssertionError("unexpected request")

    monkeypatch.setenv("VEP_BOX_ACCESS_TOKEN", TOKEN_SECRET)
    workspace = initialized_authorized_job(tmp_path, SourceKind.BOX, "12345")
    runtime = AcquisitionRuntimeConfig(
        box_token_env="VEP_BOX_ACCESS_TOKEN", maximum_download_bytes=1024
    )
    adapter = BoxAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
        sleep=sleeps.append,
    )
    result = acquire_job(workspace.root, runtime, adapter)
    assert content_calls == 2
    assert sleeps == [1]
    evidence = result.source_manifest.acquisition_evidence
    assert evidence is not None
    assert evidence.warnings == ["bounded Retry-After honored"]


def test_provider_privacy_scan_covers_job_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"privacy media"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2.0/files/12345":
            return httpx.Response(
                200,
                json={"id": "12345", "type": "file", "name": "privacy.mp4", "size": len(payload)},
            )
        return httpx.Response(200, content=payload)

    monkeypatch.setenv("VEP_BOX_ACCESS_TOKEN", TOKEN_SECRET)
    workspace = initialized_authorized_job(tmp_path, SourceKind.BOX, "12345")
    runtime = AcquisitionRuntimeConfig(
        box_token_env="VEP_BOX_ACCESS_TOKEN", maximum_download_bytes=1024
    )
    adapter = BoxAdapter(
        runtime,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    acquire_job(workspace.root, runtime, adapter)
    all_machine_files = [
        path
        for path in workspace.root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl", ".yaml"}
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in all_machine_files)
    assert TOKEN_SECRET not in text
    assert SIGNED_SECRET not in text
    assert "Authorization" not in text
    assert "Cookie" not in text
    assert (
        json.loads(workspace.source_manifest_json.read_text(encoding="utf-8"))[
            "acquisition_evidence"
        ]["credential_mode"]
        == "environment-token"
    )
