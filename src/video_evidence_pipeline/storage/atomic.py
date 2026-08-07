"""Atomic UTF-8 writes and immutable content copies."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.errors import configuration_error, integrity_error, io_error


def _temporary_path(target: Path) -> Path:
    return target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise io_error("ATOMIC_WRITE_FAILED", "atomic file write failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_new_bytes(target: Path, data: bytes) -> None:
    if target.exists():
        raise configuration_error("FILE_EXISTS", "target file already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise configuration_error("FILE_EXISTS", "target file already exists") from exc
    except OSError as exc:
        raise io_error("ATOMIC_WRITE_FAILED", "exclusive atomic file write failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(target: Path, value: BaseModel | object) -> None:
    atomic_write_bytes(target, pretty_json_bytes(value))


def read_json_model[ModelT: BaseModel](target: Path, model: type[ModelT], *, code: str) -> ModelT:
    try:
        raw = target.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        return model.model_validate(data)
    except FileNotFoundError as exc:
        raise io_error(code, "required machine artifact does not exist") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise integrity_error(code, "required machine artifact is malformed") from exc


def atomic_copy_exclusive(source: Path, target: Path) -> bool:
    """Copy to target without ever replacing an existing artifact."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return False
    temporary = _temporary_path(target)
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.link(temporary, target)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        raise io_error("SOURCE_COPY_FAILED", "source copy failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
