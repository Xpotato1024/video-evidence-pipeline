"""Local HTTPS-only proxy that validates and pins every yt-dlp destination."""

from __future__ import annotations

import select
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import cast
from urllib.parse import urlsplit

from video_evidence_pipeline.acquisition.network_policy import (
    Resolver,
    default_resolver,
    validated_network_destination,
)
from video_evidence_pipeline.errors import VepError

SocketConnector = Callable[[str, int, float], socket.socket]


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=timeout)


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    resolver: Resolver
    connector: SocketConnector
    connect_timeout_seconds: float
    maximum_elapsed_seconds: float

    def handle_error(self, _request: object, _client_address: object) -> None:
        return


class _HttpsConnectHandler(socketserver.StreamRequestHandler):
    def _reject(self) -> None:
        with suppress(OSError):
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")

    def handle(self) -> None:
        server = cast(_ProxyServer, self.server)
        self.connection.settimeout(server.connect_timeout_seconds)
        request_line = self.rfile.readline(4097)
        if len(request_line) > 4096:
            self._reject()
            return
        try:
            method, target, version = request_line.decode("ascii").strip().split(" ", 2)
        except (UnicodeError, ValueError):
            self._reject()
            return
        header_bytes = 0
        while True:
            line = self.rfile.readline(4097)
            header_bytes += len(line)
            if len(line) > 4096 or header_bytes > 16384:
                self._reject()
                return
            if line in {b"\r\n", b"\n", b""}:
                break
        if method != "CONNECT" or not version.startswith("HTTP/1."):
            self._reject()
            return
        parsed = urlsplit(f"//{target}")
        try:
            port = parsed.port
        except ValueError:
            self._reject()
            return
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or port is None
        ):
            self._reject()
            return
        try:
            _host, validated_port, addresses = validated_network_destination(
                f"https://{target}/",
                resolver=server.resolver,
            )
        except VepError:
            self._reject()
            return
        if validated_port != port:
            self._reject()
            return
        try:
            upstream = server.connector(addresses[0], port, server.connect_timeout_seconds)
        except OSError:
            self._reject()
            return
        with upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            self._relay(upstream, deadline=time.monotonic() + server.maximum_elapsed_seconds)

    def _relay(self, upstream: socket.socket, *, deadline: float) -> None:
        peers = (self.connection, upstream)
        while time.monotonic() < deadline:
            try:
                readable, _, _ = select.select(peers, [], [], 0.25)
                for source in readable:
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    destination = upstream if source is self.connection else self.connection
                    destination.sendall(data)
            except OSError:
                return


@contextmanager
def validating_https_proxy(
    *,
    resolver: Resolver = default_resolver,
    connector: SocketConnector = _connect,
    connect_timeout_seconds: float,
    maximum_elapsed_seconds: float,
) -> Iterator[str]:
    server = _ProxyServer(("127.0.0.1", 0), _HttpsConnectHandler)
    server.resolver = resolver
    server.connector = connector
    server.connect_timeout_seconds = connect_timeout_seconds
    server.maximum_elapsed_seconds = maximum_elapsed_seconds
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
