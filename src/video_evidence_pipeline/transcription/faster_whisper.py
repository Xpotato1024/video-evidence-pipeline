"""Lazy, offline-only faster-whisper adapter."""

from __future__ import annotations

import importlib.metadata
import os
from itertools import islice
from pathlib import Path
from typing import Any

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.domain.transcription import (
    AsrSegment,
    TranscriptConfidence,
    TranscriptConfidenceKind,
    TranscriptInterval,
    TranscriptionBackendIdentity,
    TranscriptionConfig,
    TranscriptSource,
)
from video_evidence_pipeline.errors import configuration_error

_REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
_MAXIMUM_MODEL_FILES = 10_000
_MAXIMUM_MODEL_BYTES = 64 * 1024 * 1024 * 1024
_MAXIMUM_WORD_PROBABILITIES_PER_SEGMENT = 4096
_MAXIMUM_TOTAL_WORD_PROBABILITIES = 2_000_000


class FasterWhisperBackend:
    """Load only an explicitly configured complete local model directory."""

    def __init__(self, model_directory: Path | None) -> None:
        self._model_directory = model_directory
        self._model: Any | None = None
        self._identity: TranscriptionBackendIdentity | None = None

    def _validated_model_files(self) -> dict[str, str]:
        directory = self._model_directory
        if directory is None:
            raise configuration_error(
                "MODEL_UNAVAILABLE", "ASR requires an explicit local model directory"
            )
        try:
            if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
                raise configuration_error(
                    "MODEL_UNAVAILABLE", "the configured local ASR model is unavailable"
                )
            for name in _REQUIRED_MODEL_FILES:
                if not (directory / name).is_file():
                    raise configuration_error(
                        "MODEL_UNAVAILABLE", "the configured local ASR model is incomplete"
                    )
            paths = list(islice(directory.rglob("*"), _MAXIMUM_MODEL_FILES + 1))
            if len(paths) > _MAXIMUM_MODEL_FILES:
                raise configuration_error("MODEL_INVALID", "the local ASR model has too many files")
            paths.sort(key=lambda item: item.relative_to(directory).as_posix())
            hashes: dict[str, str] = {}
            total_size = 0
            for path in paths:
                relative = path.relative_to(directory).as_posix()
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise configuration_error(
                        "MODEL_INVALID", "the local ASR model contains a link"
                    )
                if path.is_dir():
                    continue
                metadata = path.stat(follow_symlinks=False)
                if not path.is_file() or metadata.st_size <= 0:
                    raise configuration_error(
                        "MODEL_INVALID", "the local ASR model contains an invalid file"
                    )
                total_size += metadata.st_size
                if total_size > _MAXIMUM_MODEL_BYTES:
                    raise configuration_error("MODEL_INVALID", "the local ASR model is too large")
                hashes[relative] = sha256_file(path)
            if any(name not in hashes for name in _REQUIRED_MODEL_FILES):
                raise configuration_error(
                    "MODEL_UNAVAILABLE", "the configured local ASR model is incomplete"
                )
            return hashes
        except OSError:
            raise configuration_error(
                "MODEL_UNAVAILABLE", "the configured local ASR model is unavailable"
            ) from None

    def _load(self) -> tuple[Any, TranscriptionBackendIdentity]:
        if self._model is not None and self._identity is not None:
            return self._model, self._identity
        hashes = self._validated_model_files()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        try:
            from faster_whisper import WhisperModel

            version = importlib.metadata.version("faster-whisper")
            runtime_version = importlib.metadata.version("ctranslate2")
        except (ImportError, ModuleNotFoundError, importlib.metadata.PackageNotFoundError):
            raise configuration_error(
                "BACKEND_UNAVAILABLE", "the optional local ASR backend is not installed"
            ) from None
        try:
            model = WhisperModel(
                str(self._model_directory),
                device="cpu",
                compute_type="int8",
                local_files_only=True,
            )
            if self._validated_model_files() != hashes:
                raise configuration_error(
                    "MODEL_INVALID", "the local ASR model changed while loading"
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            raise configuration_error(
                "MODEL_INVALID", "the configured local ASR model could not be loaded"
            ) from None
        identity = TranscriptionBackendIdentity(
            source=TranscriptSource.ASR,
            name="faster-whisper",
            version=version,
            runtime=f"ctranslate2-{runtime_version}",
            model="local-directory-model",
            model_fingerprint=fingerprint(hashes),
            local_files_only=True,
        )
        self._model = model
        self._identity = identity
        return model, identity

    def identity(self) -> TranscriptionBackendIdentity:
        _, identity = self._load()
        return identity

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        intervals: tuple[TranscriptInterval, ...],
        configuration: TranscriptionConfig,
    ) -> list[AsrSegment]:
        model, _ = self._load()
        expected_model_fingerprint = self._identity.model_fingerprint if self._identity else None
        if fingerprint(self._validated_model_files()) != expected_model_fingerprint:
            raise configuration_error("MODEL_INVALID", "the local ASR model changed")
        try:
            generated, _information = model.transcribe(
                str(audio_path),
                language=language,
                word_timestamps=True,
                beam_size=configuration.asr_beam_size,
                temperature=configuration.asr_temperature,
                condition_on_previous_text=configuration.asr_condition_on_previous_text,
                clip_timestamps=",".join(
                    str(value / 1000)
                    for interval in intervals
                    for value in (interval.start_ms, interval.end_ms)
                ),
            )
            results: list[AsrSegment] = []
            total_text_chars = 0
            total_word_probabilities = 0
            for index, segment in enumerate(generated, start=1):
                if index > configuration.maximum_segments:
                    raise ValueError("ASR segment limit exceeded")
                raw_text = str(segment.text)
                total_text_chars += len(raw_text)
                if total_text_chars > configuration.maximum_total_text_chars:
                    raise ValueError("ASR text limit exceeded")
                word_probability_count = 0
                word_probability_min: float | None = None
                word_probability_max: float | None = None
                word_probability_sum = 0.0
                for word in getattr(segment, "words", None) or []:
                    probability = getattr(word, "probability", None)
                    if probability is None:
                        continue
                    word_probability_count += 1
                    total_word_probabilities += 1
                    if (
                        word_probability_count > _MAXIMUM_WORD_PROBABILITIES_PER_SEGMENT
                        or total_word_probabilities > _MAXIMUM_TOTAL_WORD_PROBABILITIES
                    ):
                        raise ValueError("ASR word metric limit exceeded")
                    probability_value = float(probability)
                    word_probability_sum += probability_value
                    word_probability_min = (
                        probability_value
                        if word_probability_min is None
                        else min(word_probability_min, probability_value)
                    )
                    word_probability_max = (
                        probability_value
                        if word_probability_max is None
                        else max(word_probability_max, probability_value)
                    )
                results.append(
                    AsrSegment(
                        start_ms=round(float(segment.start) * 1000),
                        end_ms=round(float(segment.end) * 1000),
                        raw_text=raw_text,
                        backend_segment_id=str(index),
                        confidence=TranscriptConfidence(
                            kind=TranscriptConfidenceKind.BACKEND_METRIC,
                            avg_logprob=float(segment.avg_logprob),
                            no_speech_probability=(
                                float(segment.no_speech_prob)
                                if getattr(segment, "no_speech_prob", None) is not None
                                else None
                            ),
                            word_probability_count=word_probability_count,
                            word_probability_min=word_probability_min,
                            word_probability_max=word_probability_max,
                            word_probability_mean=(
                                word_probability_sum / word_probability_count
                                if word_probability_count
                                else None
                            ),
                        ),
                    )
                )
            if fingerprint(self._validated_model_files()) != expected_model_fingerprint:
                raise configuration_error("MODEL_INVALID", "the local ASR model changed")
            return results
        except Exception as exc:
            if getattr(exc, "code", None) in {
                "BACKEND_UNAVAILABLE",
                "MODEL_UNAVAILABLE",
                "MODEL_INVALID",
            }:
                raise
            raise configuration_error(
                "TRANSCRIPTION_FAILED", "local ASR transcription failed"
            ) from None
