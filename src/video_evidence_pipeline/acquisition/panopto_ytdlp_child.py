"""Bounded Panopto-only yt-dlp entry point with typed auth signals."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from yt_dlp import YoutubeDL, parse_options
from yt_dlp.extractor.panopto import PanoptoIE
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, ExtractorError

from video_evidence_pipeline.acquisition.panopto_ytdlp_protocol import (
    PANOPTO_CHILD_EXIT_REAUTH_REQUIRED,
    PANOPTO_CHILD_EXIT_VIEWER_DENIED,
)


class PanoptoReauthenticationRequired(Exception):
    """Pinned Panopto extractor observed its typed login-required signal."""


class _VepPanoptoIE(PanoptoIE):
    IE_NAME = PanoptoIE.ie_key()

    @classmethod
    def ie_key(cls) -> str:
        return PanoptoIE.ie_key()

    def raise_login_required(self, *args: object, **kwargs: object) -> None:
        raise PanoptoReauthenticationRequired from None

    def mark_watched(self, *_args: object, **_kwargs: object) -> None:
        return None

    def _mark_watched(self, *_args: object, **_kwargs: object) -> None:
        return None


def _validated_tenant(url: str) -> str | None:
    if not PanoptoIE.suitable(url):
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in {None, 443}
        or parsed.path.lower() != "/panopto/pages/viewer.aspx"
    ):
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) != 1 or query[0][0].lower() != "id":
        return None
    try:
        UUID(query[0][1])
    except ValueError:
        return None
    host = parsed.hostname
    return host.lower() if host is not None else None


def _download_error_chain(error: BaseException):
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, DownloadError) and current.exc_info is not None:
            original = current.exc_info[1]
            if isinstance(original, BaseException):
                pending.append(original)
        if isinstance(current, ExtractorError):
            if isinstance(current.cause, BaseException):
                pending.append(current.cause)
            if current.exc_info is not None:
                original = current.exc_info[1]
                if isinstance(original, BaseException):
                    pending.append(original)


def _is_delivery_info_url(url: object, tenant: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and (parsed.hostname or "").lower() == tenant
        and parsed.path.lower() == "/panopto/pages/viewer/deliveryinfo.aspx"
    )


def classify_download_error(error: BaseException, tenant: str) -> int | None:
    """Return only a typed auth/denial code tied to Panopto's viewer API."""
    for candidate in _download_error_chain(error):
        if not isinstance(candidate, HTTPError):
            continue
        response = candidate.response
        if not _is_delivery_info_url(response.url, tenant):
            continue
        if candidate.status == 401:
            return PANOPTO_CHILD_EXIT_REAUTH_REQUIRED
        if candidate.status == 403:
            return PANOPTO_CHILD_EXIT_VIEWER_DENIED
    return None


def _run(argv: Sequence[str]) -> int:
    parsed = parse_options(list(argv))
    if len(parsed.urls) != 1:
        return 1
    url = parsed.urls[0]
    tenant = _validated_tenant(url)
    if tenant is None:
        return 1

    ydl_options = dict(parsed.ydl_opts)
    ydl_options["mark_watched"] = False
    ydl_options["ignoreerrors"] = False
    with YoutubeDL(ydl_options) as downloader:
        downloader.add_info_extractor(_VepPanoptoIE(downloader))
        try:
            downloader.extract_info(url, download=True, ie_key=_VepPanoptoIE.ie_key())
        except PanoptoReauthenticationRequired:
            return PANOPTO_CHILD_EXIT_REAUTH_REQUIRED
        except Exception as exc:
            classification = classify_download_error(exc, tenant)
            if classification is not None:
                return classification
            return 1
        return downloader._download_retcode


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _run(argv if argv is not None else sys.argv[1:])
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
