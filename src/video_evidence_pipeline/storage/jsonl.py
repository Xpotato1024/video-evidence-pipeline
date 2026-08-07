"""Append-only, newline-terminated JSONL records."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline.domain.serialization import canonical_json_bytes
from video_evidence_pipeline.errors import integrity_error, io_error


@contextmanager
def exclusive_append_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        yield
    except FileExistsError as exc:
        raise integrity_error("TRANSITION_LOG_BUSY", "transition log is being updated") from exc
    except OSError as exc:
        raise io_error("TRANSITION_LOCK_FAILED", "transition log could not be locked") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)


def append_jsonl(path: Path, record: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(record) + b"\n"
    try:
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
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
