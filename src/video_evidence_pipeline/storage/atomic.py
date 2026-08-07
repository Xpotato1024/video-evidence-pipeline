"""Atomic UTF-8 writes and immutable content copies."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from contextlib import suppress
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
    device: int | None = None
    inode: int | None = None


def cleanup_exclusive_copy(target: Path, result: ExclusiveCopyResult) -> None:
    """Best-effort removal only while the target still identifies our new inode."""
    if not result.created:
        return
    with suppress(OSError):
        current = target.stat(follow_symlinks=False)
        if (
            stat.S_ISREG(current.st_mode)
            and current.st_dev == result.device
            and current.st_ino == result.inode
        ):
            target.unlink()


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
    try:
        raw = target.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        return model.model_validate(data)
    except FileNotFoundError as exc:
        raise io_error(code, "required machine artifact does not exist") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise integrity_error(code, "required machine artifact is malformed") from exc


def atomic_copy_exclusive(source: Path, target: Path) -> ExclusiveCopyResult:
    """Copy to target without ever replacing an existing artifact."""
    ensure_private_directory(target.parent)
    if target.exists():
        return ExclusiveCopyResult(created=False)
    temporary = _temporary_path(target)
    created_target = False
    copied_identity: os.stat_result | None = None
    try:
        with (
            source.open("rb") as input_stream,
            _open_exclusive(temporary, private=True) as output_stream,
        ):
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            copied_identity = os.fstat(output_stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return ExclusiveCopyResult(created=False)
        created_target = True
        enforce_private_file(target)
        if copied_identity is None:  # pragma: no cover - copy must complete before linking
            raise OSError("copied file identity is unavailable")
        return ExclusiveCopyResult(
            created=True,
            device=copied_identity.st_dev,
            inode=copied_identity.st_ino,
        )
    except OSError as exc:
        raise io_error("SOURCE_COPY_FAILED", "source copy failed") from exc
    except Exception:
        if created_target and copied_identity is not None:
            cleanup_exclusive_copy(
                target,
                ExclusiveCopyResult(
                    created=True,
                    device=copied_identity.st_dev,
                    inode=copied_identity.st_ino,
                ),
            )
        raise
    finally:
        temporary.unlink(missing_ok=True)
