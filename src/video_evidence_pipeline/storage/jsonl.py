"""Append-only, newline-terminated JSONL records."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline.domain.serialization import canonical_json_bytes
from video_evidence_pipeline.errors import integrity_error, io_error


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
