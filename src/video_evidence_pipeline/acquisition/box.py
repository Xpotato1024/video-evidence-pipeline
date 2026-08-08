"""Box file-ID acquisition through the official metadata and content APIs."""

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
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.acquisition.staged import (
    AcquisitionEvidenceDraft,
    publish_staged_source,
)
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import JobSpec, SourceKind
from video_evidence_pipeline.domain.locators import box_file_id, locator_fingerprint
from video_evidence_pipeline.domain.manifests import CredentialMode, HttpStatusCategory
from video_evidence_pipeline.errors import integrity_error
from video_evidence_pipeline.storage.layout import JobWorkspace


class BoxAdapter(AcquisitionAdapter):
    def __init__(
        self,
        runtime: AcquisitionRuntimeConfig,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver = default_resolver,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.resolver = resolver
        self.sleep = sleep

    def probe(self, job: JobSpec) -> ProbeResult:
        if job.source.kind is not SourceKind.BOX:
            raise integrity_error("ADAPTER_KIND_MISMATCH", "Box adapter kind mismatch")
        file_id = box_file_id(job.source.locator)
        return ProbeResult(
            display_name=f"box-{file_id}.media",
            locator_fingerprint=locator_fingerprint(job.source.locator),
        )

    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        started_at = datetime.now(UTC)
        probe = self.probe(job)
        file_id = box_file_id(job.source.locator)
        token = self.runtime.token_from_environment(self.runtime.box_token_env, provider="box")
        metadata_url = f"https://api.box.com/2.0/files/{file_id}?fields=id,type,name,size"
        metadata = get_json(
            metadata_url,
            self.runtime,
            client=self.client,
            resolver=self.resolver,
            bearer_token=token,
            credential_origin=origin(metadata_url),
            error_code="BOX_API_FAILED",
            access_denied_code="BOX_ACCESS_DENIED",
            not_found_code="BOX_FILE_NOT_FOUND",
        ).data
        if str(metadata.get("id")) != file_id or metadata.get("type") != "file":
            raise integrity_error("BOX_API_INVALID", "Box metadata identity did not match")
        declared_size = metadata.get("size")
        if (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size <= 0
        ):
            raise integrity_error("BOX_API_INVALID", "Box metadata size was invalid")
        if declared_size > self.runtime.maximum_download_bytes:
            raise integrity_error("SOURCE_TOO_LARGE", "Box file exceeds the configured limit")
        name = metadata.get("name")
        display_name = (
            sanitize_filename(name)
            if isinstance(name, str) and name.strip()
            else probe.display_name
        )
        content_url = f"https://api.box.com/2.0/files/{file_id}/content"
        staged = stream_download(
            content_url,
            workspace,
            self.runtime,
            AcquisitionEvidenceDraft(
                adapter_kind=SourceKind.BOX,
                transport="https",
                backend="box-rest",
                backend_version="2.0",
                requested_host="api.box.com",
                final_host="api.box.com",
                redirect_count=0,
                http_status_category=HttpStatusCategory.SUCCESS,
                response_content_type=None,
                declared_content_length=declared_size,
                credential_mode=CredentialMode.ENVIRONMENT_TOKEN,
            ),
            client=self.client,
            resolver=self.resolver,
            bearer_token=token,
            credential_origin=origin(content_url),
            allow_sensitive_redirect_query=True,
            display_name=display_name,
            retry_attempts=2,
            maximum_retry_after_seconds=5,
            sleep=self.sleep,
            error_code="BOX_ACQUISITION_FAILED",
        )
        if staged.path.stat().st_size != declared_size:
            staged.path.unlink(missing_ok=True)
            raise integrity_error(
                "BOX_SIZE_MISMATCH", "Box metadata size did not match acquired bytes"
            )
        return publish_staged_source(
            job,
            workspace,
            staged,
            maximum_bytes=self.runtime.maximum_download_bytes,
            started_at=started_at,
        )
