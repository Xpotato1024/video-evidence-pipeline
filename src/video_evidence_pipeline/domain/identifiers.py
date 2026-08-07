"""Identifiers and path-safe value validation."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import StringConstraints

JOB_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

JobId = Annotated[str, StringConstraints(pattern=JOB_ID_PATTERN, min_length=1, max_length=63)]
Sha256 = Annotated[str, StringConstraints(pattern=SHA256_PATTERN, min_length=64, max_length=64)]


def validate_relative_storage_path(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("storage path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("storage path must not be absolute or contain traversal")
    return path.as_posix()


def sanitize_filename(name: str, *, maximum_length: int = 128) -> str:
    normalized = unicodedata.normalize("NFKC", name)
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip(" ._")
    if not sanitized:
        sanitized = "source"
    if sanitized in {".", ".."}:
        sanitized = "source"
    if PurePosixPath(sanitized).stem.upper() in WINDOWS_RESERVED_NAMES:
        sanitized = f"source_{sanitized}"
    if len(sanitized) > maximum_length:
        suffix = PurePosixPath(sanitized).suffix[:16]
        stem_limit = max(1, maximum_length - len(suffix))
        sanitized = sanitized[:stem_limit].rstrip(" ._") + suffix
    return sanitized
