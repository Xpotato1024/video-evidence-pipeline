"""Streaming and canonical SHA-256 helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def local_locator_fingerprint(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    return sha256_bytes(normalized.encode("utf-8"))
