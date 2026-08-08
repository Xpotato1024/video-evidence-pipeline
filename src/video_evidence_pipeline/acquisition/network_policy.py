"""HTTPS, DNS, redirect, and SSRF policy for network acquisition."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from video_evidence_pipeline.domain.locators import has_sensitive_query
from video_evidence_pipeline.errors import configuration_error, io_error

Resolver = Callable[[str, int], Iterable[str]]

_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def default_resolver(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise io_error("NETWORK_DNS_FAILED", "network source host could not be resolved") from exc
    return sorted({record[4][0] for record in records})


def validate_resolved_addresses(addresses: Iterable[str]) -> list[str]:
    values = list(addresses)
    if not values:
        raise io_error("NETWORK_DNS_FAILED", "network source host did not resolve")
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise io_error("NETWORK_DNS_FAILED", "network source resolution was invalid") from exc
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
) -> str:
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
    except ValueError as exc:
        raise configuration_error("NETWORK_URL_REJECTED", "network URL port is invalid") from exc
    validate_resolved_addresses(resolver(host, port))
    return host


def origin(locator: str) -> tuple[str, str, int]:
    parsed = urlsplit(locator)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 443
