"""Fail-closed private permissions for runtime workspace artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from video_evidence_pipeline.errors import io_error

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
POSIX_MODE_ENFORCED = os.name == "posix"


def _permission_failure() -> Exception:
    return io_error(
        "PRIVATE_PERMISSION_FAILED",
        "private runtime permissions could not be enforced",
    )


def enforce_private_descriptor(descriptor: int, mode: int) -> None:
    """Set and verify a descriptor mode before protected data is written."""
    if not POSIX_MODE_ENFORCED:
        return
    try:
        os.fchmod(descriptor, mode)
        actual = stat.S_IMODE(os.fstat(descriptor).st_mode)
    except OSError as exc:
        raise _permission_failure() from exc
    if actual != mode:
        raise _permission_failure()


def enforce_private_directory(path: Path) -> None:
    """Set and verify 0700 without following a final symlink."""
    if not POSIX_MODE_ENFORCED:
        return
    try:
        os.chmod(path, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
        actual = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    except (NotImplementedError, OSError) as exc:
        raise _permission_failure() from exc
    if actual != PRIVATE_DIRECTORY_MODE:
        raise _permission_failure()


def ensure_private_directory(path: Path, *, exist_ok: bool = True) -> None:
    """Create one directory level and fail if POSIX privacy cannot be enforced."""
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE, exist_ok=exist_ok)
    except OSError as exc:
        raise io_error(
            "PRIVATE_DIRECTORY_FAILED", "private directory could not be created"
        ) from exc
    enforce_private_directory(path)


def enforce_private_file(path: Path) -> None:
    """Verify a final runtime file through a non-following descriptor when available."""
    if not POSIX_MODE_ENFORCED:
        return
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise _permission_failure() from exc
