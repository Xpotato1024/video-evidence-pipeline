"""Box file-ID acquisition through the official metadata and content APIs."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from video_evidence_pipeline.acquisition.base import (
    AcquisitionAdapter,
    AcquisitionResult,
    ProbeResult,
)
from video_evidence_pipeline.acquisition.http_transport import (
    BoxApiHeader,
    get_json,
    stream_download,
)
from video_evidence_pipeline.acquisition.network_policy import Resolver, default_resolver, origin
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
)
from video_evidence_pipeline.acquisition.staged import (
    AcquisitionEvidenceDraft,
    publish_staged_source,
)
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import JobSpec, SourceKind
from video_evidence_pipeline.domain.locators import (
    BoxLocatorKind,
    box_file_id,
    box_locator_kind,
    box_shared_link,
    locator_fingerprint,
)
from video_evidence_pipeline.domain.manifests import CredentialMode, HttpStatusCategory
from video_evidence_pipeline.errors import integrity_error
from video_evidence_pipeline.storage.layout import JobWorkspace

_BOX_FILE_ID = re.compile(r"^[0-9]+$")


def _response_file_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    candidate = str(value)
    if not candidate or not candidate.isascii() or not _BOX_FILE_ID.fullmatch(candidate):
        return None
    return candidate


def _validated_file_metadata(
    data: Mapping[str, Any],
    *,
    expected_file_id: str | None,
    invalid_code: str,
    non_file_code: str | None = None,
    maximum_download_bytes: int,
) -> tuple[str, str, int]:
    if data.get("type") != "file":
        if non_file_code is not None and isinstance(data.get("type"), str):
            raise integrity_error(non_file_code, "Box shared item is not a file")
        raise integrity_error(invalid_code, "Box API response was not a file")
    file_id = _response_file_id(data.get("id"))
    if file_id is None or (expected_file_id is not None and file_id != expected_file_id):
        raise integrity_error(invalid_code, "Box API response file identity was invalid")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 1024:
        raise integrity_error(invalid_code, "Box API response file name was invalid")
    declared_size = data.get("size")
    if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size <= 0:
        raise integrity_error(invalid_code, "Box API response file size was invalid")
    if declared_size > maximum_download_bytes:
        raise integrity_error("SOURCE_TOO_LARGE", "Box file exceeds the configured limit")
    return file_id, name, declared_size


class BoxAdapter(AcquisitionAdapter):
    def __init__(
        self,
        runtime: AcquisitionRuntimeConfig,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver = default_resolver,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.resolver = resolver
        self.sleep = sleep
        self.monotonic = monotonic

    def probe(self, job: JobSpec) -> ProbeResult:
        if job.source.kind is not SourceKind.BOX:
            raise integrity_error("ADAPTER_KIND_MISMATCH", "Box adapter kind mismatch")
        if box_locator_kind(job.source.locator) is BoxLocatorKind.FILE_ID:
            display_name = f"box-{box_file_id(job.source.locator)}.media"
        else:
            display_name = "box-shared-item.media"
        return ProbeResult(
            display_name=display_name,
            locator_fingerprint=locator_fingerprint(job.source.locator),
        )

    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        started_at = datetime.now(UTC)
        deadline = AcquisitionDeadline.start(
            self.runtime.maximum_download_seconds,
            monotonic=self.monotonic,
        )
        probe = self.probe(job)
        token = self.runtime.token_from_environment(self.runtime.box_token_env, provider="box")
        locator_kind = box_locator_kind(job.source.locator)
        box_api_header = None
        if locator_kind is BoxLocatorKind.SHARED_LINK:
            box_api_header = BoxApiHeader(box_shared_link(job.source.locator))
            shared_item = get_json(
                "https://api.box.com/2.0/shared_items?fields=id,type,name,size",
                self.runtime,
                client=self.client,
                resolver=self.resolver,
                bearer_token=token,
                box_api_header=box_api_header,
                credential_origin=("https", "api.box.com", 443),
                error_code="BOX_SHARED_LINK_INVALID",
                access_denied_code="BOX_ACCESS_DENIED",
                not_found_code="BOX_SHARED_LINK_INVALID",
                deadline=deadline,
            ).data
            file_id, _shared_name, _shared_size = _validated_file_metadata(
                shared_item,
                expected_file_id=None,
                invalid_code="BOX_SHARED_LINK_INVALID",
                non_file_code="BOX_SHARED_ITEM_NOT_FILE",
                maximum_download_bytes=self.runtime.maximum_download_bytes,
            )
        else:
            file_id = box_file_id(job.source.locator)
        metadata_url = f"https://api.box.com/2.0/files/{file_id}?fields=id,type,name,size"
        metadata = get_json(
            metadata_url,
            self.runtime,
            client=self.client,
            resolver=self.resolver,
            bearer_token=token,
            box_api_header=box_api_header,
            credential_origin=origin(metadata_url),
            error_code="BOX_API_FAILED",
            access_denied_code="BOX_ACCESS_DENIED",
            not_found_code="BOX_FILE_NOT_FOUND",
            deadline=deadline,
        ).data
        _metadata_id, name, declared_size = _validated_file_metadata(
            metadata,
            expected_file_id=file_id,
            invalid_code="BOX_API_INVALID",
            maximum_download_bytes=self.runtime.maximum_download_bytes,
        )
        display_name = sanitize_filename(name) if name.strip() else probe.display_name
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
            box_api_header=box_api_header,
            credential_origin=origin(content_url),
            allow_sensitive_redirect_query=True,
            display_name=display_name,
            retry_attempts=2,
            maximum_retry_after_seconds=5,
            sleep=self.sleep,
            deadline=deadline,
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
