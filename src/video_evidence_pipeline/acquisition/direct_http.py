"""Direct stable HTTPS acquisition."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import httpx

from video_evidence_pipeline.acquisition.base import (
    AcquisitionAdapter,
    AcquisitionResult,
    ProbeResult,
)
from video_evidence_pipeline.acquisition.http_transport import stream_download
from video_evidence_pipeline.acquisition.network_policy import Resolver, default_resolver
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.acquisition.staged import (
    AcquisitionEvidenceDraft,
    publish_staged_source,
)
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import JobSpec, SourceKind
from video_evidence_pipeline.domain.locators import locator_fingerprint
from video_evidence_pipeline.domain.manifests import CredentialMode, HttpStatusCategory
from video_evidence_pipeline.errors import integrity_error
from video_evidence_pipeline.storage.layout import JobWorkspace


class DirectHttpAdapter(AcquisitionAdapter):
    def __init__(
        self,
        runtime: AcquisitionRuntimeConfig,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver = default_resolver,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.resolver = resolver

    def probe(self, job: JobSpec) -> ProbeResult:
        if job.source.kind is not SourceKind.DIRECT_HTTP:
            raise integrity_error("ADAPTER_KIND_MISMATCH", "direct HTTP adapter kind mismatch")
        path_name = unquote(urlsplit(job.source.locator).path).rsplit("/", 1)[-1]
        return ProbeResult(
            display_name=sanitize_filename(path_name or "source.bin"),
            locator_fingerprint=locator_fingerprint(job.source.locator),
        )

    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        started_at = datetime.now(UTC)
        probe = self.probe(job)
        host = (urlsplit(job.source.locator).hostname or "").lower()
        staged = stream_download(
            job.source.locator,
            workspace,
            self.runtime,
            AcquisitionEvidenceDraft(
                adapter_kind=SourceKind.DIRECT_HTTP,
                transport="https",
                backend="httpx",
                backend_version=httpx.__version__,
                requested_host=host,
                final_host=host,
                redirect_count=0,
                http_status_category=HttpStatusCategory.SUCCESS,
                response_content_type=None,
                declared_content_length=None,
                credential_mode=CredentialMode.NONE,
            ),
            client=self.client,
            resolver=self.resolver,
            display_name=probe.display_name,
        )
        return publish_staged_source(
            job,
            workspace,
            staged,
            maximum_bytes=self.runtime.maximum_download_bytes,
            started_at=started_at,
        )
