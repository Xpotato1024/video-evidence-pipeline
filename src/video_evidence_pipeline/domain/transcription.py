"""Strict contracts for transcript construction and ASR adapter results."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.media import SubtitleProvenance


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
    schema_version: Literal["1.0"] = "1.0"
    preferred_languages: list[str] = Field(default_factory=list, max_length=32)
    minimum_caption_coverage: float = Field(default=0.8, ge=0.0, le=1.0, allow_inf_nan=False)
    gap_policy: GapPolicy = GapPolicy.RECORD
    overlap_policy: OverlapPolicy = OverlapPolicy.PRESERVE
    asr_language: str | None = Field(default=None, min_length=1, max_length=64)
    asr_device: Literal["cpu"] = "cpu"
    asr_compute_type: Literal["int8"] = "int8"
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


class TranscriptionBackendIdentity(StrictModel):
    source: TranscriptSource
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    runtime: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    model_fingerprint: Sha256 | None = None
    local_files_only: Literal[True] = True

    @field_validator("name", "version", "runtime", "model")
    @classmethod
    def identity_metadata_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("transcription backend identity metadata must not be blank")
        return value


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


class TranscriptManifest(StrictModel):
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
