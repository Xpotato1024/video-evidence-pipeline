"""Append-only, newline-terminated JSONL records."""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline.domain.serialization import canonical_json_bytes
from video_evidence_pipeline.errors import integrity_error, io_error
from video_evidence_pipeline.storage.permissions import (
    PRIVATE_FILE_MODE,
    enforce_private_descriptor,
    ensure_private_directory,
)


@contextmanager
def exclusive_append_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor: int | None = None
    try:
        is_junction = getattr(lock_path, "is_junction", lambda: False)
        if lock_path.is_symlink() or is_junction():
            raise OSError("lock path must not be a link or junction")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("lock path must be a regular file")
        enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
        os.ftruncate(descriptor, 1)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _acquire_process_lock(descriptor)
        yield
    except BlockingIOError as exc:
        raise integrity_error("TRANSITION_LOG_BUSY", "transition log is being updated") from exc
    except OSError as exc:
        raise io_error("TRANSITION_LOCK_FAILED", "transition log could not be locked") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _acquire_process_lock(descriptor: int) -> None:
    deadline = time.monotonic() + 0.25
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


def append_jsonl(path: Path, record: BaseModel) -> None:
    ensure_private_directory(path.parent)
    payload = canonical_json_bytes(record) + b"\n"
    try:
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short append")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise io_error("JSONL_APPEND_FAILED", "append-only log write failed") from exc


def read_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise integrity_error("TRANSITION_LOG_MISSING", "transition log does not exist") from exc
    except OSError as exc:
        raise io_error("JSONL_READ_FAILED", "append-only log could not be read") from exc
    if not payload or not payload.endswith(b"\n"):
        raise integrity_error("JSONL_INCOMPLETE", "append-only log has an incomplete record")
    records: list[ModelT] = []
    try:
        for line in payload.splitlines():
            records.append(model.model_validate(json.loads(line.decode("utf-8"))))
    except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise integrity_error(
            "JSONL_MALFORMED", "append-only log contains a malformed record"
        ) from exc
    return records
