"""Panopto tenant/session acquisition using the official REST contract first."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from video_evidence_pipeline.acquisition.base import (
    AcquisitionAdapter,
    AcquisitionResult,
    ProbeResult,
)
from video_evidence_pipeline.acquisition.http_transport import get_json, stream_download
from video_evidence_pipeline.acquisition.network_policy import Resolver, default_resolver, origin
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
)
from video_evidence_pipeline.acquisition.staged import (
    AcquisitionEvidenceDraft,
    publish_staged_source,
)
from video_evidence_pipeline.acquisition.ytdlp_runner import (
    SubprocessRunner,
    YtdlpToolchainFactory,
    default_ytdlp_toolchain,
    run_ytdlp,
)
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import JobSpec, SourceKind
from video_evidence_pipeline.domain.locators import locator_fingerprint, panopto_identity
from video_evidence_pipeline.domain.manifests import CredentialMode, HttpStatusCategory
from video_evidence_pipeline.errors import VepError, integrity_error, state_error
from video_evidence_pipeline.storage.layout import JobWorkspace


class PanoptoAdapter(AcquisitionAdapter):
    def __init__(
        self,
        runtime: AcquisitionRuntimeConfig,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver = default_resolver,
        runner: SubprocessRunner | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        toolchain_factory: YtdlpToolchainFactory = default_ytdlp_toolchain,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.resolver = resolver
        self.runner = runner
        self.monotonic = monotonic
        self.toolchain_factory = toolchain_factory

    def probe(self, job: JobSpec) -> ProbeResult:
        if job.source.kind is not SourceKind.PANOPTO:
            raise integrity_error("ADAPTER_KIND_MISMATCH", "Panopto adapter kind mismatch")
        _tenant, session_id = panopto_identity(job.source.locator)
        return ProbeResult(
            display_name=f"panopto-{session_id}.media",
            locator_fingerprint=locator_fingerprint(job.source.locator),
        )

    def _fallback(
        self,
        job: JobSpec,
        workspace: JobWorkspace,
        *,
        started_at: datetime,
        deadline: AcquisitionDeadline,
    ) -> AcquisitionResult:
        if not self.runtime.panopto_ytdlp_fallback or self.runtime.cookies_from_browser is None:
            raise state_error(
                "PANOPTO_DOWNLOAD_UNAVAILABLE",
                "Panopto session does not expose an authorized download",
            )
        tenant, session_id = panopto_identity(job.source.locator)
        if (
            self.runtime.panopto_token_env is not None
            and self.runtime.panopto_tenant_host != tenant
        ):
            raise state_error(
                "PANOPTO_TENANT_MISMATCH",
                "Panopto token is not bound to the requested tenant",
            )
        viewer_url = f"https://{tenant}/Panopto/Pages/Viewer.aspx?id={session_id}"
        staged = run_ytdlp(
            viewer_url,
            workspace,
            self.runtime,
            adapter_kind=SourceKind.PANOPTO,
            runner=self.runner,
            resolver=self.resolver,
            deadline=deadline,
            toolchain_factory=self.toolchain_factory,
        )
        return publish_staged_source(
            job,
            workspace,
            staged,
            maximum_bytes=self.runtime.maximum_download_bytes,
            started_at=started_at,
        )

    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        started_at = datetime.now(UTC)
        deadline = AcquisitionDeadline.start(
            self.runtime.maximum_download_seconds,
            monotonic=self.monotonic,
        )
        probe = self.probe(job)
        tenant, session_id = panopto_identity(job.source.locator)
        if (
            self.runtime.panopto_token_env is not None
            and self.runtime.panopto_tenant_host != tenant
        ):
            raise state_error(
                "PANOPTO_TENANT_MISMATCH",
                "Panopto token is not bound to the requested tenant",
            )
        try:
            token = self.runtime.token_from_environment(
                self.runtime.panopto_token_env,
                provider="panopto",
            )
        except VepError as exc:
            if (
                exc.code == "PANOPTO_CREDENTIAL_REQUIRED"
                and self.runtime.panopto_token_env is None
                and self.runtime.panopto_ytdlp_fallback
            ):
                return self._fallback(
                    job,
                    workspace,
                    started_at=started_at,
                    deadline=deadline,
                )
            raise
        api_url = f"https://{tenant}/Panopto/api/v1/sessions/{session_id}"
        metadata = get_json(
            api_url,
            self.runtime,
            client=self.client,
            resolver=self.resolver,
            bearer_token=token,
            credential_origin=origin(api_url),
            error_code="PANOPTO_API_FAILED",
            access_denied_code="PANOPTO_ACCESS_DENIED",
            not_found_code="PANOPTO_SESSION_NOT_FOUND",
            deadline=deadline,
        ).data
        response_id = metadata.get("Id")
        if not isinstance(response_id, str) or response_id.lower() != session_id.lower():
            raise integrity_error("PANOPTO_API_INVALID", "Panopto session identity did not match")
        urls = metadata.get("Urls")
        download_url = urls.get("DownloadUrl") if isinstance(urls, dict) else None
        if not isinstance(download_url, str) or not download_url.strip():
            raise state_error(
                "PANOPTO_DOWNLOAD_UNAVAILABLE",
                "Panopto session does not expose an authorized download",
            )
        name = metadata.get("Name")
        display_name = (
            sanitize_filename(name)
            if isinstance(name, str) and name.strip()
            else probe.display_name
        )
        staged = stream_download(
            download_url,
            workspace,
            self.runtime,
            AcquisitionEvidenceDraft(
                adapter_kind=SourceKind.PANOPTO,
                transport="https",
                backend="panopto-rest",
                backend_version="v1",
                requested_host=tenant,
                final_host=tenant,
                redirect_count=0,
                http_status_category=HttpStatusCategory.SUCCESS,
                response_content_type=None,
                declared_content_length=None,
                credential_mode=CredentialMode.ENVIRONMENT_TOKEN,
            ),
            client=self.client,
            resolver=self.resolver,
            allow_sensitive_redirect_query=True,
            display_name=display_name,
            deadline=deadline,
            error_code="PANOPTO_ACQUISITION_FAILED",
        )
        return publish_staged_source(
            job,
            workspace,
            staged,
            maximum_bytes=self.runtime.maximum_download_bytes,
            started_at=started_at,
        )
