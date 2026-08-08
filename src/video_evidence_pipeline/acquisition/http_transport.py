"""Redacted, manually redirected HTTPX transport helpers."""

from __future__ import annotations

import json
import os
import ssl
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpcore
import httpx

from video_evidence_pipeline.acquisition.network_policy import (
    Resolver,
    default_resolver,
    origin,
    resolve_with_timeout,
    validate_network_url,
    validate_resolved_addresses,
)
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
)
from video_evidence_pipeline.acquisition.staged import (
    AcquisitionEvidenceDraft,
    StagedSource,
    new_staging_path,
    open_private_staging_file,
)
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.manifests import HttpStatusCategory
from video_evidence_pipeline.errors import configuration_error, integrity_error, io_error
from video_evidence_pipeline.storage.layout import JobWorkspace

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_RETRYABLE_STATUSES = {202, 429, 503}
_REJECTED_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "text/html",
    "text/json",
    "text/xml",
}


@dataclass(frozen=True, slots=True)
class JsonResponse:
    data: Mapping[str, Any]
    status_code: int
    host: str


class _PinnedResolverBackend(httpcore.SyncBackend):
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        addresses = validate_resolved_addresses(self._resolver(host, port))
        return self._backend.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _PinnedHttpTransport(httpx.HTTPTransport):
    def __init__(self, resolver: Resolver) -> None:
        super().__init__(verify=True, trust_env=False, retries=0)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=_PinnedResolverBackend(resolver),
            retries=0,
        )


def build_httpx_client(
    runtime: AcquisitionRuntimeConfig,
    *,
    resolver: Resolver = default_resolver,
    deadline: AcquisitionDeadline | None = None,
) -> httpx.Client:
    timeout = httpx.Timeout(
        connect=runtime.network_timeout_seconds,
        read=runtime.network_timeout_seconds,
        write=runtime.network_timeout_seconds,
        pool=runtime.network_timeout_seconds,
    )

    def bounded_resolver(host: str, port: int) -> list[str]:
        remaining = (
            deadline.remaining() if deadline is not None else runtime.network_timeout_seconds
        )
        return resolve_with_timeout(
            resolver,
            host,
            port,
            timeout_seconds=min(runtime.network_timeout_seconds, remaining),
        )

    return httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        verify=True,
        trust_env=False,
        transport=_PinnedHttpTransport(bounded_resolver),
    )


def _request_timeout(
    runtime: AcquisitionRuntimeConfig,
    deadline: AcquisitionDeadline,
) -> httpx.Timeout:
    remaining = min(runtime.network_timeout_seconds, deadline.remaining())
    return httpx.Timeout(connect=remaining, read=remaining, write=remaining, pool=remaining)


def _resolve_timeout(
    runtime: AcquisitionRuntimeConfig,
    deadline: AcquisitionDeadline,
) -> float:
    return min(runtime.network_timeout_seconds, deadline.remaining())


def _headers_for(
    locator: str,
    *,
    credential_origin: tuple[str, str, int] | None,
    bearer_token: str | None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/octet-stream,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "User-Agent": "video-evidence-pipeline/0.1",
    }
    if bearer_token is not None and credential_origin == origin(locator):
        headers["Authorization"] = f"Bearer {bearer_token}"
    return headers


def _content_type(response: httpx.Response) -> str | None:
    value = response.headers.get("content-type")
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _declared_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise integrity_error(
            "HTTP_CONTENT_INVALID", "declared content length is invalid"
        ) from None
    if parsed < 0:
        raise integrity_error("HTTP_CONTENT_INVALID", "declared content length is invalid")
    return parsed


def _looks_like_error_document(prefix: bytes) -> bool:
    visible = prefix.lstrip().lower()
    return visible.startswith((b"<!doctype html", b"<html", b"<?xml", b"{", b"["))


def _default_display_name(locator: str) -> str:
    name = PurePosixPath(unquote(urlsplit(locator).path)).name
    return sanitize_filename(name or "source.bin")


def stream_download(
    requested_url: str,
    workspace: JobWorkspace,
    runtime: AcquisitionRuntimeConfig,
    evidence: AcquisitionEvidenceDraft,
    *,
    client: httpx.Client | None = None,
    resolver: Resolver = default_resolver,
    bearer_token: str | None = None,
    credential_origin: tuple[str, str, int] | None = None,
    allow_sensitive_redirect_query: bool = False,
    display_name: str | None = None,
    retry_attempts: int = 0,
    maximum_retry_after_seconds: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    deadline: AcquisitionDeadline | None = None,
    error_code: str = "HTTP_ACQUISITION_FAILED",
) -> StagedSource:
    acquisition_deadline = deadline or AcquisitionDeadline.start(
        runtime.maximum_download_seconds,
        monotonic=monotonic,
    )
    owned_client = client is None
    active_client = client or build_httpx_client(
        runtime,
        resolver=resolver,
        deadline=acquisition_deadline,
    )
    current = requested_url
    validate_network_url(
        current,
        resolver=resolver,
        allow_sensitive_query=allow_sensitive_redirect_query,
        resolve_timeout_seconds=_resolve_timeout(runtime, acquisition_deadline),
    )
    visited: set[str] = set()
    redirect_count = 0
    retries = 0
    warnings = list(evidence.warnings)
    staged_path = new_staging_path(workspace)
    try:
        while True:
            acquisition_deadline.remaining()
            final_host = validate_network_url(
                current,
                resolver=resolver,
                allow_sensitive_query=allow_sensitive_redirect_query,
                resolve_timeout_seconds=_resolve_timeout(runtime, acquisition_deadline),
            )
            if current in visited:
                raise configuration_error(
                    "HTTP_REDIRECT_LOOP", "network redirect loop was rejected"
                )
            visited.add(current)
            try:
                response_context: AbstractContextManager[httpx.Response] = active_client.stream(
                    "GET",
                    current,
                    headers=_headers_for(
                        current,
                        credential_origin=credential_origin,
                        bearer_token=bearer_token,
                    ),
                    follow_redirects=False,
                    timeout=_request_timeout(runtime, acquisition_deadline),
                )
                with response_context as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise io_error(error_code, "network redirect did not include a target")
                        if redirect_count >= runtime.maximum_redirects:
                            raise configuration_error(
                                "HTTP_REDIRECT_LIMIT", "network redirect limit was exceeded"
                            )
                        next_url = urljoin(current, location)
                        validate_network_url(
                            next_url,
                            resolver=resolver,
                            allow_sensitive_query=allow_sensitive_redirect_query,
                            resolve_timeout_seconds=_resolve_timeout(
                                runtime,
                                acquisition_deadline,
                            ),
                        )
                        if (
                            urlsplit(current).scheme == "https"
                            and urlsplit(next_url).scheme != "https"
                        ):
                            raise configuration_error(
                                "HTTP_REDIRECT_DOWNGRADE", "HTTPS redirect downgrade was rejected"
                            )
                        current = next_url
                        redirect_count += 1
                        continue
                    if (
                        response.status_code in _RETRYABLE_STATUSES
                        and retries < retry_attempts
                        and response.headers.get("retry-after") is not None
                    ):
                        try:
                            delay = int(response.headers["retry-after"])
                        except ValueError:
                            raise io_error(
                                error_code, "network retry instruction was invalid"
                            ) from None
                        if delay < 0 or delay > maximum_retry_after_seconds:
                            raise io_error(
                                error_code, "network retry instruction exceeded its bound"
                            )
                        retries += 1
                        warnings.append("bounded Retry-After honored")
                        visited.remove(current)
                        if delay >= acquisition_deadline.remaining():
                            raise io_error(
                                "NETWORK_TIMEOUT", "network acquisition deadline was exceeded"
                            )
                        sleep(delay)
                        continue
                    if response.status_code != 200:
                        raise io_error(error_code, "network source request failed")
                    content_type = _content_type(response)
                    content_encoding = response.headers.get("content-encoding", "identity").lower()
                    if content_encoding not in {"", "identity"}:
                        raise integrity_error(
                            "HTTP_CONTENT_REJECTED", "encoded response is not accepted as raw media"
                        )
                    if content_type in _REJECTED_MEDIA_TYPES or (
                        content_type is not None
                        and (content_type.endswith("+json") or content_type.endswith("+xml"))
                    ):
                        raise integrity_error(
                            "HTTP_CONTENT_REJECTED", "error document is not accepted as raw media"
                        )
                    declared_length = _declared_length(response)
                    if (
                        declared_length is not None
                        and declared_length > runtime.maximum_download_bytes
                    ):
                        raise integrity_error(
                            "SOURCE_TOO_LARGE", "declared source size exceeds the configured limit"
                        )
                    actual = 0
                    prefix = bytearray()
                    with open_private_staging_file(staged_path) as output:
                        chunks = (
                            [response.content]
                            if response.is_stream_consumed
                            else response.iter_raw()
                        )
                        for chunk in chunks:
                            acquisition_deadline.remaining()
                            if not chunk:
                                continue
                            actual += len(chunk)
                            if actual > runtime.maximum_download_bytes:
                                raise integrity_error(
                                    "SOURCE_TOO_LARGE",
                                    "acquired source exceeded the configured limit",
                                )
                            if len(prefix) < 512:
                                prefix.extend(chunk[: 512 - len(prefix)])
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if actual <= 0:
                        raise integrity_error("SOURCE_EMPTY", "network source was empty")
                    if declared_length is not None and declared_length != actual:
                        raise integrity_error(
                            "HTTP_CONTENT_LENGTH_MISMATCH",
                            "declared content length did not match acquired bytes",
                        )
                    if _looks_like_error_document(bytes(prefix)):
                        raise integrity_error(
                            "HTTP_CONTENT_REJECTED", "error document is not accepted as raw media"
                        )
                    return StagedSource(
                        path=staged_path,
                        display_name=display_name or _default_display_name(requested_url),
                        evidence=AcquisitionEvidenceDraft(
                            adapter_kind=evidence.adapter_kind,
                            transport=evidence.transport,
                            backend=evidence.backend,
                            backend_version=evidence.backend_version,
                            requested_host=evidence.requested_host,
                            final_host=final_host,
                            redirect_count=redirect_count,
                            http_status_category=HttpStatusCategory.SUCCESS,
                            response_content_type=content_type,
                            declared_content_length=(
                                declared_length
                                if declared_length is not None
                                else evidence.declared_content_length
                            ),
                            credential_mode=evidence.credential_mode,
                            warnings=tuple(warnings),
                        ),
                    )
            except httpx.TimeoutException:
                raise io_error("NETWORK_TIMEOUT", "network source request timed out") from None
            except httpx.RequestError:
                raise io_error(error_code, "network source request failed") from None
    except Exception:
        with suppress(OSError):
            staged_path.unlink(missing_ok=True)
        raise
    finally:
        if owned_client:
            active_client.close()


def get_json(
    locator: str,
    runtime: AcquisitionRuntimeConfig,
    *,
    client: httpx.Client | None = None,
    resolver: Resolver = default_resolver,
    bearer_token: str,
    credential_origin: tuple[str, str, int],
    error_code: str,
    access_denied_code: str | None = None,
    not_found_code: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    deadline: AcquisitionDeadline | None = None,
) -> JsonResponse:
    acquisition_deadline = deadline or AcquisitionDeadline.start(
        runtime.maximum_download_seconds,
        monotonic=monotonic,
    )
    host = validate_network_url(
        locator,
        resolver=resolver,
        resolve_timeout_seconds=_resolve_timeout(runtime, acquisition_deadline),
    )
    owned_client = client is None
    active_client = client or build_httpx_client(
        runtime,
        resolver=resolver,
        deadline=acquisition_deadline,
    )
    try:
        try:
            with active_client.stream(
                "GET",
                locator,
                headers={
                    **_headers_for(
                        locator,
                        credential_origin=credential_origin,
                        bearer_token=bearer_token,
                    ),
                    "Accept": "application/json",
                },
                follow_redirects=False,
                timeout=_request_timeout(runtime, acquisition_deadline),
            ) as response:
                if response.status_code in {401, 403}:
                    raise io_error(
                        access_denied_code or error_code, "network API access was denied"
                    )
                if response.status_code == 404:
                    raise io_error(
                        not_found_code or error_code, "network API resource was not found"
                    )
                if response.status_code in _REDIRECT_STATUSES or response.status_code != 200:
                    raise io_error(error_code, "network API request failed")
                content_encoding = response.headers.get("content-encoding", "identity").lower()
                if content_encoding not in {"", "identity"}:
                    raise integrity_error(
                        "API_RESPONSE_INVALID",
                        "encoded network API response was rejected",
                    )
                content = bytearray()
                chunks = [response.content] if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    acquisition_deadline.remaining()
                    if len(chunk) > (1024 * 1024) - len(content):
                        raise integrity_error(
                            "API_RESPONSE_INVALID", "network API response exceeded its limit"
                        )
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise io_error("NETWORK_TIMEOUT", "network API request timed out") from None
        except httpx.RequestError:
            raise io_error(error_code, "network API request failed") from None
        try:
            data = json.loads(bytes(content).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise integrity_error(
                "API_RESPONSE_INVALID", "network API response was invalid"
            ) from None
        if not isinstance(data, dict):
            raise integrity_error("API_RESPONSE_INVALID", "network API response was invalid")
        return JsonResponse(data=data, status_code=response.status_code, host=host)
    finally:
        if owned_client:
            active_client.close()
