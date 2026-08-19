"""Strict contracts for transcript construction and ASR adapter results."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.media import SubtitleProvenance

MAXIMUM_ASR_WORKER_REQUEST_BYTES = 4 * 1024 * 1024


class TranscriptSource(StrEnum):
    SUPPLIED = "supplied"
    NORMALIZED_SUBTITLE = "normalized_subtitle"
    ASR = "asr"


class GapPolicy(StrEnum):
    RECORD = "record"
    REJECT = "reject"


class OverlapPolicy(StrEnum):
    PRESERVE = "preserve"
    REJECT = "reject"


class TranscriptConfidenceKind(StrEnum):
    NOT_AVAILABLE = "not_available"
    BACKEND_METRIC = "backend_metric"


class DeviceSelectionReason(StrEnum):
    EXPLICIT_CPU = "explicit_cpu"
    NO_GPU_VISIBLE = "no_gpu_visible"
    CONTAINER_PASSTHROUGH_UNAVAILABLE = "container_passthrough_unavailable"
    BACKEND_CUDA_UNAVAILABLE = "backend_cuda_unavailable"
    GPU_COMPUTE_TYPE_UNSUPPORTED = "gpu_compute_type_unsupported"
    INSUFFICIENT_FREE_VRAM = "insufficient_free_vram"
    AUTO_GPU_SELECTED = "auto_gpu_selected"
    EXPLICIT_CUDA_SELECTED = "explicit_cuda_selected"
    GPU_RESOURCE_FALLBACK = "gpu_resource_fallback"


class TranscriptConfidence(StrictModel):
    """Raw confidence metadata, deliberately not a calibrated probability."""

    kind: TranscriptConfidenceKind
    avg_logprob: float | None = Field(default=None, allow_inf_nan=False)
    no_speech_probability: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    word_probability_count: int = Field(default=0, ge=0, le=1_000_000)
    word_probability_min: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    word_probability_max: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    word_probability_mean: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def fields_match_kind(self) -> Self:
        if self.kind is TranscriptConfidenceKind.NOT_AVAILABLE:
            if (
                self.avg_logprob is not None
                or self.no_speech_probability is not None
                or self.word_probability_count != 0
                or self.word_probability_min is not None
                or self.word_probability_max is not None
                or self.word_probability_mean is not None
            ):
                raise ValueError("unavailable confidence cannot contain a backend metric")
        elif self.avg_logprob is None:
            raise ValueError("ASR confidence requires the backend avg_logprob metric")
        summaries = (
            self.word_probability_min,
            self.word_probability_max,
            self.word_probability_mean,
        )
        if self.word_probability_count == 0 and any(value is not None for value in summaries):
            raise ValueError("empty word probability summary must not contain values")
        if self.word_probability_count > 0 and any(value is None for value in summaries):
            raise ValueError("word probability summary is incomplete")
        if (
            self.word_probability_min is not None
            and self.word_probability_max is not None
            and self.word_probability_mean is not None
            and not (
                self.word_probability_min <= self.word_probability_mean <= self.word_probability_max
            )
        ):
            raise ValueError("word probability summary is inconsistent")
        return self


class SegmentLineage(StrictModel):
    source_artifact: ArtifactRef
    source_stream_index: int | None = Field(default=None, ge=0)
    subtitle_provenance: SubtitleProvenance | None = None
    source_record_id: str = Field(min_length=1, max_length=256)
    backend_identity_fingerprint: Sha256


class TranscriptSegment(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    segment_id: str = Field(pattern=r"^T[0-9]{6}$")
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    speaker: str | None = Field(default=None, min_length=1, max_length=256)
    raw_text: str = Field(min_length=1, max_length=65536)
    normalized_text: str = Field(min_length=1, max_length=65536)
    source: TranscriptSource
    confidence: TranscriptConfidence
    flags: list[str] = Field(default_factory=list, max_length=64)
    lineage: SegmentLineage

    @field_validator("speaker", "raw_text", "normalized_text")
    @classmethod
    def text_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("transcript text fields must not be blank")
        return value

    @field_validator("flags")
    @classmethod
    def flags_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            not item.strip() or len(item) > 128 for item in value
        ):
            raise ValueError("transcript flags must be sorted, unique, and non-blank")
        return value

    @model_validator(mode="after")
    def interval_and_lineage_match(self) -> Self:
        has_interval = self.start_ms is not None and self.end_ms is not None
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("transcript segment timestamps must be both present or absent")
        if (
            has_interval
            and self.end_ms is not None
            and self.start_ms is not None
            and self.end_ms <= self.start_ms
        ):
            raise ValueError("transcript segment must end after it starts")
        if self.source is TranscriptSource.SUPPLIED:
            if (
                self.lineage.source_stream_index is not None
                or self.lineage.subtitle_provenance is not None
                or self.confidence.kind is not TranscriptConfidenceKind.NOT_AVAILABLE
            ):
                raise ValueError("supplied transcript lineage is invalid")
            return self
        if not has_interval:
            raise ValueError("media transcript segments require timestamps")
        if self.source is TranscriptSource.NORMALIZED_SUBTITLE:
            if self.lineage.source_stream_index is None or self.lineage.subtitle_provenance is None:
                raise ValueError("subtitle transcript lineage requires stream and provenance")
        elif (
            self.lineage.source_stream_index is not None
            or self.lineage.subtitle_provenance is not None
        ):
            raise ValueError("ASR transcript lineage must not claim subtitle metadata")
        if (
            self.source is TranscriptSource.NORMALIZED_SUBTITLE
            and self.confidence.kind is not TranscriptConfidenceKind.NOT_AVAILABLE
        ):
            raise ValueError("caption confidence must remain unavailable")
        if (
            self.source is TranscriptSource.ASR
            and self.confidence.kind is not TranscriptConfidenceKind.BACKEND_METRIC
        ):
            raise ValueError("ASR segment requires typed raw backend confidence")
        return self


class TranscriptInterval(StrictModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    segment_ids: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("segment_ids")
    @classmethod
    def segment_ids_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(
            len(item) != 7 or not item.startswith("T") or not item[1:].isdigit() for item in value
        ):
            raise ValueError("transcript interval segment IDs are invalid")
        return value

    @model_validator(mode="after")
    def end_is_after_start(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("transcript interval must end after it starts")
        return self


class TranscriptionConfig(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"asr_profile": {"type": "string"}},
                        "required": ["asr_profile"],
                    },
                    "then": {"properties": {"asr_compute_type": {"const": "auto"}}},
                }
            ]
        }
    )
    schema_version: Literal["1.0"] = "1.0"
    preferred_languages: list[str] = Field(default_factory=list, max_length=32)
    minimum_caption_coverage: float = Field(default=0.8, ge=0.0, le=1.0, allow_inf_nan=False)
    gap_policy: GapPolicy = GapPolicy.RECORD
    overlap_policy: OverlapPolicy = OverlapPolicy.PRESERVE
    asr_language: str | None = Field(default=None, min_length=1, max_length=64)
    asr_profile: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,127}$",
        description="Repository-owned profile ID; absent only for legacy manifests/tests.",
    )
    asr_device: str = Field(default="auto", pattern=r"^(auto|cpu|cuda|cuda:[0-9]+)$")
    asr_compute_type: Literal["auto", "int8", "float16"] = "auto"
    asr_beam_size: Literal[5] = 5
    asr_temperature: Literal[0.0] = 0.0
    asr_condition_on_previous_text: Literal[False] = False
    maximum_segments: int = Field(default=100_000, ge=1, le=1_000_000)
    maximum_total_text_chars: int = Field(default=64_000_000, ge=1, le=256_000_000)

    @field_validator("preferred_languages")
    @classmethod
    def languages_are_ordered_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 64 for item in normalized):
            raise ValueError("preferred language is invalid")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("preferred languages must be unique")
        return normalized

    @model_validator(mode="after")
    def profiled_compute_type_is_unambiguous(self) -> Self:
        if self.asr_profile is not None and self.asr_compute_type != "auto":
            raise ValueError("profiled ASR requires asr_compute_type=auto")
        return self


class TranscriptionBackendIdentity(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "source": {"const": "asr"},
                            "model_repository": {"type": "string"},
                        },
                        "required": ["source", "model_repository"],
                    },
                    "then": {
                        "properties": {
                            field: {"not": {"type": "null"}}
                            for field in (
                                "runtime",
                                "runtime_version",
                                "model",
                                "model_repository",
                                "model_revision",
                                "model_storage_identity",
                                "device_policy",
                                "selection_reason",
                                "selected_device",
                                "compute_type",
                                "container_image",
                                "container_image_digest",
                                "fallback",
                                "configured_min_free_vram_mib",
                            )
                        },
                        "required": [
                            "runtime",
                            "runtime_version",
                            "model",
                            "model_repository",
                            "model_revision",
                            "model_storage_identity",
                            "device_policy",
                            "selection_reason",
                            "selected_device",
                            "compute_type",
                            "container_image",
                            "container_image_digest",
                            "fallback",
                            "configured_min_free_vram_mib",
                        ],
                    },
                }
            ]
        }
    )
    source: TranscriptSource
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    runtime: str | None = Field(default=None, min_length=1, max_length=128)
    runtime_version: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    model_repository: str | None = Field(default=None, min_length=1, max_length=256)
    model_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    model_fingerprint: Sha256 | None = None
    model_storage_identity: Sha256 | None = None
    device_policy: str | None = Field(default=None, pattern=r"^(auto|cpu|cuda|cuda:[0-9]+)$")
    selection_reason: DeviceSelectionReason | None = None
    selected_device: Literal["cpu", "cuda"] | None = None
    gpu_index: int | None = Field(default=None, ge=0)
    gpu_name: str | None = Field(default=None, min_length=1, max_length=256)
    total_vram_mib: int | None = Field(default=None, ge=0)
    free_vram_at_selection_mib: int | None = Field(default=None, ge=0)
    configured_min_free_vram_mib: int | None = Field(default=None, ge=0)
    candidate_gpu_index: int | None = Field(default=None, ge=0)
    candidate_gpu_name: str | None = Field(default=None, min_length=1, max_length=256)
    candidate_total_vram_mib: int | None = Field(default=None, ge=0)
    candidate_free_vram_mib: int | None = Field(default=None, ge=0)
    compute_type: str | None = Field(default=None, min_length=1, max_length=64)
    container_image: str | None = Field(default=None, min_length=1, max_length=512)
    container_image_digest: str | None = Field(default=None, min_length=1, max_length=256)
    fallback: bool | None = None
    fallback_from: Literal["cuda"] | None = None
    fallback_reason: str | None = Field(default=None, min_length=1, max_length=512)
    local_files_only: Literal[True] = True

    @field_validator(
        "name",
        "version",
        "runtime",
        "runtime_version",
        "model",
        "model_repository",
        "gpu_name",
        "candidate_gpu_name",
    )
    @classmethod
    def identity_metadata_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("transcription backend identity metadata must not be blank")
        return value

    @model_validator(mode="after")
    def provenance_is_consistent(self) -> Self:
        if (
            self.selected_device == "cpu"
            and self.gpu_index is not None
            and self.fallback is not True
        ):
            raise ValueError("CPU ASR identity cannot claim a GPU index")
        if self.selected_device == "cuda" and self.gpu_index is None:
            raise ValueError("CUDA ASR identity requires a GPU index")
        if self.fallback is True and (self.fallback_from != "cuda" or self.fallback_reason is None):
            raise ValueError("ASR fallback provenance is incomplete")
        if self.fallback is not True and (
            self.fallback_from is not None or self.fallback_reason is not None
        ):
            raise ValueError("non-fallback ASR identity cannot claim fallback details")
        candidate_values = (
            self.candidate_gpu_index,
            self.candidate_gpu_name,
            self.candidate_total_vram_mib,
            self.candidate_free_vram_mib,
        )
        if any(value is not None for value in candidate_values) and any(
            value is None for value in candidate_values
        ):
            raise ValueError("ASR candidate GPU provenance is incomplete")
        return self


class AsrSegment(StrictModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    raw_text: str = Field(min_length=1, max_length=65536)
    speaker: str | None = Field(default=None, min_length=1, max_length=256)
    backend_segment_id: str = Field(min_length=1, max_length=256)
    confidence: TranscriptConfidence
    flags: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def interval_is_valid(self) -> Self:
        if self.end_ms <= self.start_ms or not self.raw_text.strip():
            raise ValueError("ASR segment is invalid")
        return self


class AsrWorkerRequest(StrictModel):
    """Bounded, provider-neutral request consumed only by the ASR inference worker."""

    schema_version: Literal["1.0"] = "1.0"
    audio_path: Literal["/input/audio"] = "/input/audio"
    model_path: Literal["/model"] = "/model"
    intervals: list[TranscriptInterval] = Field(min_length=1, max_length=100_000)
    language_hint: str | None = Field(default=None, min_length=1, max_length=64)
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    engine: Literal["faster-whisper"]
    engine_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    runtime: Literal["CTranslate2"]
    runtime_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    model: Literal["large-v3"]
    model_repository: str = Field(min_length=1, max_length=256)
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    model_storage_identity: Sha256
    model_file_count: int = Field(ge=1, le=100_000)
    model_total_size_bytes: int = Field(ge=1, le=128 * 1024 * 1024 * 1024)
    model_provisioned_at: UtcDateTime
    requested_device_policy: str = Field(pattern=r"^(auto|cpu|cuda|cuda:[0-9]+)$")
    container_gpu_passthrough: bool
    gpu_unavailable_reason: DeviceSelectionReason | None = None
    gpu_compute_type: Literal["float16"]
    cpu_compute_type: Literal["int8"]
    configured_min_free_vram_mib: int = Field(ge=1, le=1_048_576)
    container_image: str = Field(min_length=1, max_length=512)
    container_image_digest: str = Field(pattern=r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")
    configuration: TranscriptionConfig

    @model_validator(mode="after")
    def profile_and_configuration_match(self) -> Self:
        if (
            self.configuration.asr_profile != self.profile_id
            or self.configuration.asr_device != self.requested_device_policy
        ):
            raise ValueError("ASR worker request configuration does not match its profile")
        if self.requested_device_policy == "cpu":
            if self.container_gpu_passthrough or self.gpu_unavailable_reason is not None:
                raise ValueError("explicit CPU worker request must not contain GPU probe results")
        elif self.container_gpu_passthrough == (self.gpu_unavailable_reason is not None):
            raise ValueError("ASR worker GPU availability provenance is inconsistent")
        elif self.gpu_unavailable_reason not in {
            None,
            DeviceSelectionReason.NO_GPU_VISIBLE,
            DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE,
            DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE,
            DeviceSelectionReason.GPU_COMPUTE_TYPE_UNSUPPORTED,
        }:
            raise ValueError("ASR worker GPU preflight reason is invalid")
        return self


class AsrWorkerResponse(StrictModel):
    """Untrusted worker output validated by the host before canonical publication."""

    schema_version: Literal["1.0"] = "1.0"
    backend: TranscriptionBackendIdentity
    segments: list[AsrSegment] = Field(max_length=100_000)
    diagnostics: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def response_is_asr(self) -> Self:
        if self.backend.source is not TranscriptSource.ASR:
            raise ValueError("ASR worker response requires an ASR backend identity")
        if self.diagnostics != sorted(set(self.diagnostics)) or any(
            not item.strip() or len(item) > 512 for item in self.diagnostics
        ):
            raise ValueError("ASR worker diagnostics are invalid")
        return self


class TranscriptManifest(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "configuration": {
                                "properties": {"asr_profile": {"type": "string"}},
                                "required": ["asr_profile"],
                            },
                            "backends": {
                                "contains": {
                                    "properties": {"source": {"const": "asr"}},
                                    "required": ["source"],
                                }
                            },
                        },
                        "required": ["configuration", "backends"],
                    },
                    "then": {
                        "properties": {
                            "backends": {
                                "contains": {
                                    "properties": {
                                        "source": {"const": "asr"},
                                        **{
                                            field: {"not": {"type": "null"}}
                                            for field in (
                                                "runtime",
                                                "model",
                                                "model_revision",
                                                "model_storage_identity",
                                                "device_policy",
                                                "selected_device",
                                                "configured_min_free_vram_mib",
                                                "compute_type",
                                                "container_image",
                                                "container_image_digest",
                                                "fallback",
                                            )
                                        },
                                    },
                                    "allOf": [
                                        {
                                            "if": {
                                                "anyOf": [
                                                    {"required": [field]}
                                                    for field in (
                                                        "runtime_version",
                                                        "model_repository",
                                                        "selection_reason",
                                                        "candidate_gpu_index",
                                                        "candidate_gpu_name",
                                                        "candidate_total_vram_mib",
                                                        "candidate_free_vram_mib",
                                                    )
                                                ]
                                            },
                                            "then": {
                                                "properties": {
                                                    field: {"not": {"type": "null"}}
                                                    for field in (
                                                        "runtime_version",
                                                        "model_repository",
                                                        "selection_reason",
                                                    )
                                                },
                                                "required": [
                                                    "runtime_version",
                                                    "model_repository",
                                                    "selection_reason",
                                                ],
                                            },
                                        }
                                    ],
                                    "required": [
                                        "source",
                                        "name",
                                        "version",
                                        "runtime",
                                        "model",
                                        "model_revision",
                                        "model_fingerprint",
                                        "model_storage_identity",
                                        "device_policy",
                                        "selected_device",
                                        "gpu_index",
                                        "gpu_name",
                                        "total_vram_mib",
                                        "free_vram_at_selection_mib",
                                        "configured_min_free_vram_mib",
                                        "compute_type",
                                        "container_image",
                                        "container_image_digest",
                                        "fallback",
                                        "fallback_from",
                                        "fallback_reason",
                                        "local_files_only",
                                    ],
                                }
                            }
                        }
                    },
                }
            ]
        }
    )

    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    transcription_fingerprint: Sha256
    media_bundle_fingerprint: Sha256
    implementation_version: str = Field(min_length=1, max_length=128)
    configuration: TranscriptionConfig
    backends: list[TranscriptionBackendIdentity] = Field(min_length=1, max_length=8)
    selected_source_artifacts: list[ArtifactRef] = Field(min_length=1, max_length=32)
    selected_subtitle_stream_index: int | None = Field(default=None, ge=0)
    selected_subtitle_provenance: SubtitleProvenance | None = None
    caption_coverage: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    segments_artifact: ArtifactRef
    segment_count: int = Field(ge=1)
    duration_ms: int | None = Field(default=None, gt=0)
    gaps: list[TranscriptInterval] = Field(default_factory=list)
    overlaps: list[TranscriptInterval] = Field(default_factory=list)
    started_at: UtcDateTime
    created_at: UtcDateTime
    warnings: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("selected_source_artifacts")
    @classmethod
    def sources_are_unique(cls, value: list[ArtifactRef]) -> list[ArtifactRef]:
        if len({item.content_sha256 for item in value}) != len(value):
            raise ValueError("selected transcript source artifacts must be unique")
        return value

    @field_validator("warnings")
    @classmethod
    def warnings_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("transcript warnings must be sorted and unique")
        return value

    @model_validator(mode="after")
    def bundle_metadata_is_consistent(self) -> Self:
        if self.created_at < self.started_at:
            raise ValueError("transcript manifest timestamps are inconsistent")
        backend_sources = {item.source for item in self.backends}
        asr_backend = next(
            (item for item in self.backends if item.source is TranscriptSource.ASR), None
        )
        current_profile_fields = {
            "runtime_version",
            "model_repository",
            "selection_reason",
            "candidate_gpu_index",
            "candidate_gpu_name",
            "candidate_total_vram_mib",
            "candidate_free_vram_mib",
        }
        audited_pr46_fields = {
            "source",
            "name",
            "version",
            "runtime",
            "model",
            "model_revision",
            "model_fingerprint",
            "model_storage_identity",
            "device_policy",
            "selected_device",
            "gpu_index",
            "gpu_name",
            "total_vram_mib",
            "free_vram_at_selection_mib",
            "configured_min_free_vram_mib",
            "compute_type",
            "container_image",
            "container_image_digest",
            "fallback",
            "fallback_from",
            "fallback_reason",
            "local_files_only",
        }
        audited_pr46_compatibility = (
            asr_backend is not None
            and not (current_profile_fields & asr_backend.model_fields_set)
            and audited_pr46_fields <= asr_backend.model_fields_set
        )
        if (
            self.configuration.asr_profile is not None
            and asr_backend is not None
            and not audited_pr46_compatibility
        ):
            required = (
                asr_backend.runtime,
                asr_backend.runtime_version,
                asr_backend.model,
                asr_backend.model_repository,
                asr_backend.model_revision,
                asr_backend.model_storage_identity,
                asr_backend.device_policy,
                asr_backend.selection_reason,
                asr_backend.selected_device,
                asr_backend.compute_type,
                asr_backend.container_image,
                asr_backend.container_image_digest,
                asr_backend.fallback,
                asr_backend.configured_min_free_vram_mib,
            )
            if any(value is None for value in required):
                raise ValueError("profiled ASR backend provenance is incomplete")
            if asr_backend.model_repository != asr_backend.model:
                raise ValueError("profiled ASR model repository identity is inconsistent")
            if (
                asr_backend.fallback is True
                and asr_backend.selection_reason is not DeviceSelectionReason.GPU_RESOURCE_FALLBACK
            ):
                raise ValueError("profiled ASR fallback selection reason is inconsistent")
            if asr_backend.selected_device == "cuda" and (
                asr_backend.gpu_index is None
                or asr_backend.gpu_name is None
                or asr_backend.total_vram_mib is None
                or asr_backend.free_vram_at_selection_mib is None
            ):
                raise ValueError("profiled CUDA provenance is incomplete")
        if len({item.source for item in self.backends}) != len(self.backends):
            raise ValueError("transcription backend identities must be unique")
        expected_backend_order = [
            source
            for source in (
                TranscriptSource.SUPPLIED,
                TranscriptSource.NORMALIZED_SUBTITLE,
                TranscriptSource.ASR,
            )
            if source in backend_sources
        ]
        if [item.source for item in self.backends] != expected_backend_order:
            raise ValueError("transcription backend identities are not canonical")
        if TranscriptSource.SUPPLIED in backend_sources:
            if (
                backend_sources != {TranscriptSource.SUPPLIED}
                or self.selected_subtitle_stream_index is not None
                or self.selected_subtitle_provenance is not None
                or self.caption_coverage is not None
            ):
                raise ValueError("supplied transcript metadata is inconsistent")
            if self.duration_ms is None:
                if self.gaps or self.overlaps:
                    raise ValueError("timestamp-less supplied transcript cannot claim intervals")
                return self
        elif self.duration_ms is None:
            raise ValueError("media transcript requires a duration")
        if TranscriptSource.NORMALIZED_SUBTITLE in backend_sources:
            if (
                self.selected_subtitle_stream_index is None
                or self.selected_subtitle_provenance is None
                or self.caption_coverage is None
            ):
                raise ValueError("caption transcript requires stream, provenance, and coverage")
        elif (
            self.selected_subtitle_stream_index is not None
            or self.selected_subtitle_provenance is not None
            or self.caption_coverage is not None
        ):
            raise ValueError("ASR-only transcript cannot claim caption selection metadata")
        if any(item.segment_ids for item in self.gaps):
            raise ValueError("transcript gaps must not claim segment IDs")
        if any(len(item.segment_ids) < 2 for item in self.overlaps):
            raise ValueError("transcript overlaps require at least two segment IDs")
        if self.gaps != sorted(self.gaps, key=lambda item: (item.start_ms, item.end_ms)) or any(
            following.start_ms < current.end_ms
            for current, following in zip(self.gaps, self.gaps[1:], strict=False)
        ):
            raise ValueError("transcript gaps must be canonical and non-overlapping")
        if self.overlaps != sorted(
            self.overlaps,
            key=lambda item: (item.start_ms, item.end_ms, item.segment_ids),
        ) or any(
            following.start_ms < current.end_ms
            for current, following in zip(self.overlaps, self.overlaps[1:], strict=False)
        ):
            raise ValueError("transcript overlaps must be canonical and non-overlapping")
        if any(item.end_ms > self.duration_ms for item in [*self.gaps, *self.overlaps]):
            raise ValueError("transcript interval exceeds media duration")
        return self
