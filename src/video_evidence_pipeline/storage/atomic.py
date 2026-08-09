"""Atomic UTF-8 writes and immutable content copies."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.errors import configuration_error, integrity_error, io_error
from video_evidence_pipeline.storage.permissions import (
    PRIVATE_FILE_MODE,
    enforce_private_descriptor,
    enforce_private_file,
    ensure_private_directory,
)


def _temporary_path(target: Path) -> Path:
    return target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"


@dataclass(frozen=True, slots=True)
class ExclusiveCopyResult:
    created: bool


def _open_exclusive(path: Path, *, private: bool):
    mode = PRIVATE_FILE_MODE if private else 0o666
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        if private:
            enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
        return os.fdopen(descriptor, "wb")
    except Exception:
        os.close(descriptor)
        raise


def _ensure_parent(target: Path, *, private: bool) -> None:
    if private:
        ensure_private_directory(target.parent)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_bytes(target: Path, data: bytes, *, private: bool = False) -> None:
    _ensure_parent(target, private=private)
    temporary = _temporary_path(target)
    try:
        with _open_exclusive(temporary, private=private) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if private:
            enforce_private_file(target)
    except OSError as exc:
        raise io_error("ATOMIC_WRITE_FAILED", "atomic file write failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_new_bytes(target: Path, data: bytes, *, private: bool = False) -> None:
    if target.exists():
        raise configuration_error("FILE_EXISTS", "target file already exists")
    _ensure_parent(target, private=private)
    temporary = _temporary_path(target)
    try:
        with _open_exclusive(temporary, private=private) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        if private:
            enforce_private_file(target)
    except FileExistsError as exc:
        raise configuration_error("FILE_EXISTS", "target file already exists") from exc
    except OSError as exc:
        raise io_error("ATOMIC_WRITE_FAILED", "exclusive atomic file write failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(
    target: Path,
    value: BaseModel | object,
    *,
    private: bool = False,
) -> None:
    atomic_write_bytes(target, pretty_json_bytes(value), private=private)


def read_json_model[ModelT: BaseModel](target: Path, model: type[ModelT], *, code: str) -> ModelT:
    error = None
    try:
        raw = target.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        result = model.model_validate(data)
    except FileNotFoundError:
        error = io_error(code, "required machine artifact does not exist")
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        error = integrity_error(code, "required machine artifact is malformed")
    if error is not None:
        raise error
    return result


def atomic_copy_exclusive(
    source: Path,
    target: Path,
    *,
    verifier: Callable[[Path], None] | None = None,
) -> ExclusiveCopyResult:
    """Verify a private temporary copy before exclusively publishing its hard link."""
    ensure_private_directory(target.parent)
    if target.exists():
        return ExclusiveCopyResult(created=False)
    temporary = _temporary_path(target)
    operation_failed = False
    try:
        with (
            source.open("rb") as input_stream,
            _open_exclusive(temporary, private=True) as output_stream,
        ):
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if verifier is not None:
            verifier(temporary)
        try:
            os.link(temporary, target)
        except FileExistsError:
            return ExclusiveCopyResult(created=False)
        enforce_private_file(target)
        return ExclusiveCopyResult(created=True)
    except OSError as exc:
        operation_failed = True
        raise io_error("SOURCE_COPY_FAILED", "source copy failed") from exc
    except Exception:
        operation_failed = True
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            if not operation_failed:
                raise io_error(
                    "SOURCE_COPY_CLEANUP_FAILED",
                    "temporary source copy could not be removed",
                ) from exc
