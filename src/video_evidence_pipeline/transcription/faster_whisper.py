"""Lazy, offline-only faster-whisper adapter."""

from __future__ import annotations

import importlib.metadata
import os
from dataclasses import dataclass
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
from video_evidence_pipeline.transcription.device import (
    GpuDevice,
    RuntimeCapabilities,
    is_gpu_resource_error,
    probe_nvidia_gpus,
    select_device,
)
from video_evidence_pipeline.transcription.profiles import (
    AsrModelProvisionManifest,
    AsrProfile,
)

_REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
_MAXIMUM_MODEL_FILES = 10_000
_MAXIMUM_MODEL_BYTES = 64 * 1024 * 1024 * 1024
_MAXIMUM_WORD_PROBABILITIES_PER_SEGMENT = 4096
_MAXIMUM_TOTAL_WORD_PROBABILITIES = 2_000_000


@dataclass(frozen=True, slots=True)
class _ModelSnapshot:
    metadata: tuple[tuple[str, int, int, int, int], ...]
    hashes: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ResolvedRuntime:
    device: str
    compute_type: str
    device_index: int | None
    gpu: GpuDevice | None = None


_MODEL_FINGERPRINT_CACHE: dict[Path, _ModelSnapshot] = {}


class FasterWhisperBackend:
    """Load only an explicitly configured complete local model directory."""

    def __init__(
        self,
        model_directory: Path | None,
        *,
        device: str = "auto",
        compute_type: str = "auto",
        profile: AsrProfile | None = None,
        provision_manifest: AsrModelProvisionManifest | None = None,
        container_image: str | None = None,
        container_image_digest: str | None = None,
        capability_override: RuntimeCapabilities | None = None,
    ) -> None:
        self._model_directory = model_directory
        self._requested_device = device
        self._requested_compute_type = compute_type
        self._profile = profile
        self._provision_manifest = provision_manifest
        self._container_image = container_image
        self._container_image_digest = container_image_digest
        self._capability_override = capability_override
        self._forced_cpu_fallback_reason: str | None = None
        self._fallback_gpu: GpuDevice | None = None
        self._runtime_gpu: GpuDevice | None = None
        self._model: Any | None = None
        self._identity: TranscriptionBackendIdentity | None = None

    def _model_metadata(
        self,
    ) -> tuple[Path, tuple[Path, ...], tuple[tuple[str, int, int, int, int], ...]]:
        directory = self._model_directory
        if directory is None:
            raise configuration_error(
                "MODEL_UNAVAILABLE", "ASR requires an explicit local model directory"
            )
        if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
            raise configuration_error(
                "MODEL_UNAVAILABLE", "the configured local ASR model is unavailable"
            )
        resolved = directory.resolve(strict=True)
        for name in _REQUIRED_MODEL_FILES:
            if not (resolved / name).is_file():
                raise configuration_error(
                    "MODEL_UNAVAILABLE", "the configured local ASR model is incomplete"
                )
        paths = list(islice(resolved.rglob("*"), _MAXIMUM_MODEL_FILES + 1))
        if len(paths) > _MAXIMUM_MODEL_FILES:
            raise configuration_error("MODEL_INVALID", "the local ASR model has too many files")
        paths.sort(key=lambda item: item.relative_to(resolved).as_posix())
        files: list[Path] = []
        metadata_rows: list[tuple[str, int, int, int, int]] = []
        total_size = 0
        for path in paths:
            relative = path.relative_to(resolved).as_posix()
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise configuration_error("MODEL_INVALID", "the local ASR model contains a link")
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
            files.append(path)
            metadata_rows.append(
                (
                    relative,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
            )
        if any(name not in {item[0] for item in metadata_rows} for name in _REQUIRED_MODEL_FILES):
            raise configuration_error(
                "MODEL_UNAVAILABLE", "the configured local ASR model is incomplete"
            )
        return resolved, tuple(files), tuple(metadata_rows)

    def _validated_model_files(self) -> dict[str, str]:
        try:
            directory, files, metadata = self._model_metadata()
            cached = _MODEL_FINGERPRINT_CACHE.get(directory)
            if cached is not None and cached.metadata == metadata:
                return dict(cached.hashes)
            hashes = {path.relative_to(directory).as_posix(): sha256_file(path) for path in files}
            _directory_after, _files_after, metadata_after = self._model_metadata()
            if metadata_after != metadata:
                raise configuration_error("MODEL_INVALID", "the local ASR model changed")
            _MODEL_FINGERPRINT_CACHE[directory] = _ModelSnapshot(metadata, hashes)
            return hashes
        except OSError:
            raise configuration_error(
                "MODEL_UNAVAILABLE", "the configured local ASR model is unavailable"
            ) from None

    def _resolve_runtime(self, runtime: Any) -> _ResolvedRuntime:
        requested_device = self._requested_device
        requested_compute = self._requested_compute_type
        if requested_device not in {"auto", "cpu", "cuda"} and not requested_device.startswith(
            "cuda:"
        ):
            raise configuration_error("ASR_DEVICE_INVALID", "ASR device is invalid")
        if requested_compute not in {"auto", "int8", "float16"}:
            raise configuration_error("ASR_COMPUTE_TYPE_INVALID", "ASR compute type is invalid")

        if self._forced_cpu_fallback_reason is not None:
            return _ResolvedRuntime(device="cpu", compute_type="int8", device_index=None)
        if self._profile is not None:
            if self._capability_override is None:
                try:
                    cuda_count = int(runtime.get_cuda_device_count())
                    detected_gpus = probe_nvidia_gpus()
                    compatible_gpus = tuple(
                        gpu
                        for gpu in detected_gpus
                        if gpu.index < cuda_count
                        and self._profile.gpu_compute_type
                        in runtime.get_supported_compute_types("cuda", gpu.index)
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    cuda_count = 0
                    compatible_gpus = ()
                capabilities = RuntimeCapabilities(
                    gpus=compatible_gpus,
                    container_gpu_passthrough=cuda_count > 0,
                    ctranslate2_cuda_available=cuda_count > 0,
                    cuda_compute_types=(
                        frozenset({self._profile.gpu_compute_type})
                        if compatible_gpus
                        else frozenset()
                    ),
                )
            else:
                capabilities = self._capability_override
            selected = select_device(self._profile, requested_device, capabilities)
            return _ResolvedRuntime(
                device=selected.selected_device,
                compute_type=selected.compute_type,
                device_index=selected.gpu.index if selected.gpu is not None else None,
                gpu=selected.gpu,
            )

        device = requested_device
        supported: set[str]
        if requested_device == "cpu":
            device = "cpu"
        else:
            try:
                cuda_count = int(runtime.get_cuda_device_count())
                cuda_supported = (
                    set(runtime.get_supported_compute_types("cuda", 0)) if cuda_count > 0 else set()
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                cuda_count = 0
                cuda_supported = set()
            if requested_device.startswith("cuda"):
                if cuda_count <= 0 or not cuda_supported:
                    raise configuration_error(
                        "ASR_DEVICE_UNAVAILABLE", "CUDA is not usable by CTranslate2"
                    )
                device = "cuda"
            else:
                device = "cuda" if cuda_count > 0 and cuda_supported else "cpu"

        try:
            supported = set(runtime.get_supported_compute_types(device, 0))
        except (OSError, RuntimeError, TypeError, ValueError):
            if requested_device == "auto" and device == "cuda":
                device = "cpu"
                supported = set(runtime.get_supported_compute_types("cpu", 0))
            else:
                raise configuration_error(
                    "ASR_COMPUTE_TYPE_UNAVAILABLE",
                    "the requested ASR compute type could not be probed",
                ) from None
        if requested_compute == "auto":
            preferred = "float16" if device == "cuda" else "int8"
            alternate = "int8" if device == "cuda" else None
            compute_type = preferred if preferred in supported else alternate
            if compute_type is None or compute_type not in supported:
                raise configuration_error(
                    "ASR_COMPUTE_TYPE_UNAVAILABLE",
                    "no supported automatic ASR compute type is available",
                )
        else:
            compute_type = requested_compute
            if compute_type not in supported:
                raise configuration_error(
                    "ASR_COMPUTE_TYPE_UNAVAILABLE",
                    "the requested ASR compute type is unsupported on the selected device",
                )
        return _ResolvedRuntime(
            device=device,
            compute_type=compute_type,
            device_index=0 if device == "cuda" else None,
        )

    def _load(self) -> tuple[Any, TranscriptionBackendIdentity]:
        if self._model is not None and self._identity is not None:
            return self._model, self._identity
        hashes = self._validated_model_files()
        model_storage_identity = fingerprint(hashes)
        if (
            self._provision_manifest is not None
            and self._provision_manifest.model_storage_identity != model_storage_identity
        ):
            raise configuration_error(
                "MODEL_INVALID", "the provisioned ASR model does not match its manifest"
            )
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        try:
            import ctranslate2
            from faster_whisper import WhisperModel

            version = importlib.metadata.version("faster-whisper")
            runtime_version = importlib.metadata.version("ctranslate2")
        except (ImportError, ModuleNotFoundError, importlib.metadata.PackageNotFoundError):
            raise configuration_error(
                "BACKEND_UNAVAILABLE", "the optional local ASR backend is not installed"
            ) from None
        if self._profile is not None and (
            version != self._profile.engine_version
            or runtime_version != self._profile.runtime_version
        ):
            raise configuration_error(
                "ASR_RUNTIME_VERSION_MISMATCH",
                "installed ASR engine/runtime versions do not match the repository profile",
            )
        resolved = self._resolve_runtime(ctranslate2)

        def construct_model(runtime: _ResolvedRuntime) -> Any:
            model_options: dict[str, object] = {
                "device": runtime.device,
                "compute_type": runtime.compute_type,
                "local_files_only": True,
            }
            if runtime.device_index is not None:
                model_options["device_index"] = runtime.device_index
            return WhisperModel(
                str(self._model_directory),
                **model_options,
            )

        try:
            model = construct_model(resolved)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if (
                resolved.device == "cuda"
                and self._requested_device == "auto"
                and is_gpu_resource_error(exc)
            ):
                self._forced_cpu_fallback_reason = str(exc)[:512]
                self._fallback_gpu = resolved.gpu
                resolved = _ResolvedRuntime(device="cpu", compute_type="int8", device_index=None)
                try:
                    model = construct_model(resolved)
                except (OSError, RuntimeError, TypeError, ValueError):
                    raise configuration_error(
                        "MODEL_INVALID", "the configured local ASR model could not be loaded"
                    ) from None
            elif resolved.device == "cuda":
                raise configuration_error(
                    "ASR_DEVICE_UNAVAILABLE", "CUDA model loading failed"
                ) from None
            else:
                raise configuration_error(
                    "MODEL_INVALID", "the configured local ASR model could not be loaded"
                ) from None
        try:
            if self._validated_model_files() != hashes:
                raise configuration_error(
                    "MODEL_INVALID", "the local ASR model changed while loading"
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            raise configuration_error(
                "MODEL_INVALID", "the local ASR model changed while loading"
            ) from None
        self._runtime_gpu = resolved.gpu
        provenance_gpu = resolved.gpu or self._fallback_gpu
        identity = TranscriptionBackendIdentity(
            source=TranscriptSource.ASR,
            name="faster-whisper",
            version=version,
            runtime=(
                f"ctranslate2-{runtime_version};device={resolved.device};"
                f"compute_type={resolved.compute_type};"
                f"device_index={resolved.device_index if resolved.device_index is not None else 'none'}"
            ),
            model=(
                self._profile.model_repository
                if self._profile is not None
                else "local-directory-model"
            ),
            model_revision=(self._profile.model_revision if self._profile is not None else None),
            model_fingerprint=fingerprint(hashes),
            model_storage_identity=(
                self._provision_manifest.model_storage_identity
                if self._provision_manifest is not None
                else model_storage_identity
            ),
            device_policy=self._requested_device,
            selected_device=resolved.device,
            gpu_index=(
                provenance_gpu.index
                if provenance_gpu is not None
                else (resolved.device_index if resolved.device == "cuda" else None)
            ),
            gpu_name=provenance_gpu.name if provenance_gpu is not None else None,
            total_vram_mib=(provenance_gpu.total_vram_mib if provenance_gpu is not None else None),
            free_vram_at_selection_mib=(
                provenance_gpu.free_vram_mib if provenance_gpu is not None else None
            ),
            configured_min_free_vram_mib=(
                self._profile.min_free_vram_mib if self._profile is not None else None
            ),
            compute_type=resolved.compute_type,
            container_image=self._container_image,
            container_image_digest=self._container_image_digest,
            fallback=self._forced_cpu_fallback_reason is not None,
            fallback_from=("cuda" if self._forced_cpu_fallback_reason is not None else None),
            fallback_reason=self._forced_cpu_fallback_reason,
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
        model, identity = self._load()
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
            if (
                self._requested_device == "auto"
                and identity.selected_device == "cuda"
                and self._forced_cpu_fallback_reason is None
                and is_gpu_resource_error(exc)
            ):
                self._forced_cpu_fallback_reason = str(exc)[:512]
                self._fallback_gpu = self._runtime_gpu
                self._model = None
                self._identity = None
                return self.transcribe(
                    audio_path,
                    language=language,
                    intervals=intervals,
                    configuration=configuration,
                )
            if getattr(exc, "code", None) in {
                "BACKEND_UNAVAILABLE",
                "MODEL_UNAVAILABLE",
                "MODEL_INVALID",
            }:
                raise
            raise configuration_error(
                "TRANSCRIPTION_FAILED", "local ASR transcription failed"
            ) from None
