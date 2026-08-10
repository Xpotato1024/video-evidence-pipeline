"""Stable source locator canonicalization without credential persistence."""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from video_evidence_pipeline.domain.hashing import sha256_bytes

_SENSITIVE_QUERY_NAMES = {
    "access_token",
    "api-key",
    "apikey",
    "api_key",
    "awsaccesskeyid",
    "auth",
    "auth_token",
    "authorization",
    "bearer",
    "client_secret",
    "code",
    "credential",
    "expires",
    "hdnea",
    "hdnts",
    "hmac",
    "jwt",
    "key",
    "key-pair-id",
    "oauth_token",
    "password",
    "policy",
    "session",
    "session-id",
    "sessionid",
    "sig",
    "signature",
    "ticket",
    "token",
}
_SENSITIVE_QUERY_PREFIXES = ("x-amz-", "x-goog-")
_SENSITIVE_QUERY_SUFFIXES = (
    "auth",
    "credential",
    "secret",
    "signature",
    "token",
)
_AZURE_SAS_QUERY_NAMES = {
    "se",
    "sig",
    "sip",
    "skoid",
    "sks",
    "skt",
    "sktid",
    "skv",
    "sp",
    "spr",
    "sr",
    "sv",
}
_STABLE_QUERY_NAMES = {
    "course",
    "end",
    "feature",
    "fields",
    "id",
    "index",
    "lang",
    "list",
    "locale",
    "page",
    "quality",
    "start",
    "t",
    "v",
    "video",
}
_BOX_FILE_ID = re.compile(r"^[0-9]+$")
_BOX_SHARED_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_BOX_ENTERPRISE_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ent\.box\.com$")


class BoxLocatorKind(StrEnum):
    FILE_ID = "file_id"
    SHARED_LINK = "shared_link"


def is_sensitive_query_name(name: str) -> bool:
    normalized = name.strip().lower().replace("_", "-")
    aliases = {item.replace("_", "-") for item in _SENSITIVE_QUERY_NAMES}
    return (
        normalized in aliases
        or normalized in _AZURE_SAS_QUERY_NAMES
        or normalized.startswith(_SENSITIVE_QUERY_PREFIXES)
        or normalized.endswith(_SENSITIVE_QUERY_SUFFIXES)
    )


def has_sensitive_query(locator: str) -> bool:
    pairs = parse_qsl(urlsplit(locator).query, keep_blank_values=True)
    normalized_names = {name.strip().lower().replace("_", "-") for name, _ in pairs}
    return any(is_sensitive_query_name(name) for name, _ in pairs) or any(
        not name or name not in _STABLE_QUERY_NAMES for name in normalized_names
    )


def _canonical_host(parsed) -> tuple[str, str]:
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source locator must not contain userinfo")
    host = parsed.hostname
    if not host:
        raise ValueError("source locator must contain a host")
    try:
        canonical_host = host.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("source locator host is invalid") from exc
    netloc = canonical_host if port in {None, 443} else f"{canonical_host}:{port}"
    return canonical_host, netloc


def canonicalize_https_locator(locator: str, *, allow_query: bool = True) -> str:
    parsed = urlsplit(locator)
    if parsed.scheme.lower() != "https":
        raise ValueError("network source locator must use HTTPS")
    _, netloc = _canonical_host(parsed)
    if parsed.fragment:
        raise ValueError("source locator must not contain a fragment")
    if parsed.query and not allow_query:
        raise ValueError("source locator query is not supported")
    if has_sensitive_query(locator):
        raise ValueError("source locator must not contain credential or signed query parameters")
    path = parsed.path or "/"
    query = urlencode(parse_qsl(parsed.query, keep_blank_values=True), doseq=True)
    return urlunsplit(("https", netloc, path, query, ""))


def canonicalize_panopto_locator(locator: str) -> str:
    parsed = urlsplit(locator)
    if parsed.scheme.lower() == "panopto":
        host, _ = _canonical_host(parsed)
        match = re.fullmatch(r"/session/([0-9a-fA-F-]{36})", parsed.path)
        if not match or parsed.query or parsed.fragment:
            raise ValueError("Panopto locator must identify one tenant session")
        session_id = UUID(match.group(1))
        return f"panopto://{host}/session/{session_id}"
    stable = canonicalize_https_locator(locator)
    parsed = urlsplit(stable)
    if not parsed.path.lower().endswith("/panopto/pages/viewer.aspx"):
        raise ValueError("Panopto source must be a viewer URL")
    identifiers = [value for name, value in parse_qsl(parsed.query) if name.lower() == "id"]
    if len(identifiers) != 1:
        raise ValueError("Panopto viewer URL must contain one session id")
    try:
        session_id = UUID(identifiers[0])
    except ValueError as exc:
        raise ValueError("Panopto session id is invalid") from exc
    return f"panopto://{parsed.hostname}/session/{session_id}"


def canonicalize_box_locator(locator: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in locator):
        raise ValueError("Box locator must not contain control characters")
    candidate = locator.strip()
    if _BOX_FILE_ID.fullmatch(candidate):
        return f"box://file/{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() == "box":
        match = re.fullmatch(r"/([0-9]+)", parsed.path)
        if parsed.netloc.lower() != "file" or not match or parsed.query or parsed.fragment:
            raise ValueError("Box locator must identify one file")
        return f"box://file/{match.group(1)}"

    if parsed.scheme.lower() == "https" and parsed.path.startswith("/s/"):
        if "?" in candidate or "#" in candidate:
            raise ValueError("Box shared link must not contain a query or fragment")
        stable = canonicalize_https_locator(candidate, allow_query=False)
        parsed = urlsplit(stable)
        host = parsed.hostname or ""
        if parsed.port not in {None, 443}:
            raise ValueError("Box shared link must use the default HTTPS port")
        if host not in {"app.box.com", "www.box.com"} and not _BOX_ENTERPRISE_HOST.fullmatch(host):
            raise ValueError("Box shared link host is not permitted")
        match = re.fullmatch(r"/s/([^/]+)", parsed.path)
        if not match or not _BOX_SHARED_TOKEN.fullmatch(match.group(1)):
            raise ValueError("Box shared link token is invalid")
        return stable

    stable = canonicalize_https_locator(candidate)
    parsed = urlsplit(stable)
    host = parsed.hostname or ""
    if host != "app.box.com" and not host.endswith(".app.box.com"):
        raise ValueError("Box file URL must use an app.box.com host")
    match = re.fullmatch(r"/(?:files|file)/([0-9]+)(?:/.*)?", parsed.path)
    if not match:
        raise ValueError("Box URL must contain a file id")
    return f"box://file/{match.group(1)}"


def locator_fingerprint(locator: str) -> str:
    return sha256_bytes(locator.encode("utf-8"))


def panopto_identity(locator: str) -> tuple[str, str]:
    parsed = urlsplit(canonicalize_panopto_locator(locator))
    return parsed.hostname or "", parsed.path.rsplit("/", 1)[-1]


def box_locator_kind(locator: str) -> BoxLocatorKind:
    canonical = canonicalize_box_locator(locator)
    if canonical.startswith("box://file/"):
        return BoxLocatorKind.FILE_ID
    return BoxLocatorKind.SHARED_LINK


def box_file_id(locator: str) -> str:
    canonical = canonicalize_box_locator(locator)
    if not canonical.startswith("box://file/"):
        raise ValueError("Box locator does not identify a file ID")
    return urlsplit(canonical).path.rsplit("/", 1)[-1]


def box_shared_link(locator: str) -> str:
    canonical = canonicalize_box_locator(locator)
    if canonical.startswith("box://file/"):
        raise ValueError("Box locator does not identify a shared link")
    return canonical
