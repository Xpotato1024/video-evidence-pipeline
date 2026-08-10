"""Provider-neutral local ASR boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from video_evidence_pipeline.domain.transcription import (
    AsrSegment,
    TranscriptInterval,
    TranscriptionBackendIdentity,
    TranscriptionConfig,
)


class AsrBackend(Protocol):
    def identity(self) -> TranscriptionBackendIdentity: ...

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        intervals: tuple[TranscriptInterval, ...],
        configuration: TranscriptionConfig,
    ) -> list[AsrSegment]: ...
