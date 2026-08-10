"""Short-lived, fail-closed cross-process file locks for private workspaces."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from video_evidence_pipeline.errors import integrity_error, io_error
from video_evidence_pipeline.storage.permissions import (
    PRIVATE_FILE_MODE,
    enforce_private_descriptor,
)


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    busy_code: str,
    busy_message: str,
    failure_code: str,
    failure_message: str,
    timeout_seconds: float = 0.25,
) -> Iterator[None]:
    """Hold a private regular-file lock without deriving stage-specific error codes.

    Callers supply their own stable error contract.  Closing the descriptor releases
    the platform lock after a normal return or a hard process exit.
    """
    if timeout_seconds <= 0:
        raise io_error(failure_code, failure_message)
    descriptor: int | None = None
    try:
        is_junction = getattr(path, "is_junction", lambda: False)
        if path.is_symlink() or is_junction():
            raise OSError("lock path must not be a link or junction")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("lock path must be a regular file")
        enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
        os.ftruncate(descriptor, 1)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _acquire_process_lock(descriptor, timeout_seconds=timeout_seconds)
        yield
    except BlockingIOError as exc:
        raise integrity_error(busy_code, busy_message) from exc
    except OSError as exc:
        raise io_error(failure_code, failure_message) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _acquire_process_lock(descriptor: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            _try_process_lock(descriptor)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise BlockingIOError from exc
            time.sleep(0.01)


def _try_process_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
