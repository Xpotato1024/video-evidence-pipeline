"""HTTPS, DNS, redirect, and SSRF policy for network acquisition."""

from __future__ import annotations

import ipaddress
import queue
import socket
import threading
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from video_evidence_pipeline.domain.locators import has_sensitive_query
from video_evidence_pipeline.errors import VepError, configuration_error, io_error

Resolver = Callable[[str, int], Iterable[str]]

_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def default_resolver(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise io_error("NETWORK_DNS_FAILED", "network source host could not be resolved") from None
    return sorted({record[4][0] for record in records})


def resolve_with_timeout(
    resolver: Resolver,
    host: str,
    port: int,
    *,
    timeout_seconds: float | None,
) -> list[str]:
    if timeout_seconds is None:
        return list(resolver(host, port))
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put((True, list(resolver(host, port))))
        except Exception as exc:
            result.put((False, exc))

    thread = threading.Thread(target=resolve, daemon=True)
    thread.start()
    try:
        succeeded, value = result.get(timeout=timeout_seconds)
    except queue.Empty:
        raise io_error("NETWORK_TIMEOUT", "network DNS resolution timed out") from None
    if succeeded:
        return value
    if isinstance(value, VepError):
        raise value
    raise io_error("NETWORK_DNS_FAILED", "network source host could not be resolved") from None


def validate_resolved_addresses(addresses: Iterable[str]) -> list[str]:
    values = list(addresses)
    if not values:
        raise io_error("NETWORK_DNS_FAILED", "network source host did not resolve")
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise io_error("NETWORK_DNS_FAILED", "network source resolution was invalid") from None
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise configuration_error("NETWORK_SSRF_REJECTED", "network destination is not public")
    return values


def validate_network_url(
    locator: str,
    *,
    resolver: Resolver = default_resolver,
    allow_sensitive_query: bool = False,
    resolve_timeout_seconds: float | None = None,
) -> str:
    host, _port, _addresses = validated_network_destination(
        locator,
        resolver=resolver,
        allow_sensitive_query=allow_sensitive_query,
        resolve_timeout_seconds=resolve_timeout_seconds,
    )
    return host


def validated_network_destination(
    locator: str,
    *,
    resolver: Resolver = default_resolver,
    allow_sensitive_query: bool = False,
    resolve_timeout_seconds: float | None = None,
) -> tuple[str, int, list[str]]:
    parsed = urlsplit(locator)
    if parsed.scheme.lower() != "https":
        raise configuration_error("NETWORK_URL_REJECTED", "network access requires HTTPS")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise configuration_error("NETWORK_URL_REJECTED", "network URL is not permitted")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        raise configuration_error("NETWORK_SSRF_REJECTED", "network destination is not public")
    if has_sensitive_query(locator) and not allow_sensitive_query:
        raise configuration_error(
            "NETWORK_SIGNED_URL_REJECTED",
            "credential or signed query parameters are not permitted for this adapter",
        )
    try:
        port = parsed.port or 443
    except ValueError:
        raise configuration_error("NETWORK_URL_REJECTED", "network URL port is invalid") from None
    addresses = validate_resolved_addresses(
        resolve_with_timeout(
            resolver,
            host,
            port,
            timeout_seconds=resolve_timeout_seconds,
        )
    )
    return host, port, addresses


def origin(locator: str) -> tuple[str, str, int]:
    parsed = urlsplit(locator)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 443
