"""Private output reservation for FFmpeg staging files."""

from __future__ import annotations

import os
from pathlib import Path

from video_evidence_pipeline.errors import io_error
from video_evidence_pipeline.storage.permissions import (
    PRIVATE_FILE_MODE,
    enforce_private_descriptor,
    ensure_private_directory,
)


def prepare_private_output(path: Path) -> None:
    ensure_private_directory(path.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
    except OSError:
        raise io_error("NORMALIZATION_STAGING_FAILED", "private output staging failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
