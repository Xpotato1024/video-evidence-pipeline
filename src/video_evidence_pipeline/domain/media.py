"""Strict contracts for deterministic media normalization outputs."""

from __future__ import annotations

import re
from enum import StrEnum
from math import gcd
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.job import PrimaryInputKind


class MediaStreamType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"


class SubtitleKind(StrEnum):
    TEXT = "text"
    BITMAP = "bitmap"
    UNKNOWN = "unknown"


class SubtitleStatus(StrEnum):
    NORMALIZED = "normalized"
    UNSUPPORTED = "unsupported"


# These codecs have a bounded FFmpeg-to-WebVTT normalization path. This is not
# a source-text fidelity claim: decoder/container presentation and control
# markup may be changed or discarded by the transcode.
NORMALIZABLE_TEXT_SUBTITLE_CODEC_ALLOWLIST = frozenset({"srt", "subrip", "text", "webvtt"})


class SubtitleProvenance(StrEnum):
    EMBEDDED = "embedded"
    OFFICIAL = "official"
    SIDECAR = "sidecar"


class FrameReason(StrEnum):
    PERIODIC = "periodic"
    SCENE_CHANGE = "scene-change"


class ToolIdentity(StrictModel):
    name: Literal["ffmpeg", "ffprobe"]
    version: str = Field(min_length=1, max_length=256)
    executable_sha256: Sha256
    capabilities: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("tool capabilities must be sorted and unique")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_-]*:[a-z0-9][a-z0-9._-]*", item) or len(item) > 128
            for item in value
        ):
            raise ValueError("tool capability is invalid")
        return value


class MediaRational(StrictModel):
    numerator: int = Field(gt=0, le=2_147_483_647)
    denominator: int = Field(gt=0, le=2_147_483_647)

    @model_validator(mode="after")
    def is_reduced(self) -> Self:
        if gcd(self.numerator, self.denominator) != 1:
            raise ValueError("media rational must use canonical reduced form")
        return self


class MediaStream(StrictModel):
    index: int = Field(ge=0)
    stream_type: MediaStreamType
    codec_name: str = Field(min_length=1, max_length=128)
    time_base: MediaRational
    frame_rate: MediaRational | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    start_time_ms: int | None = Field(default=None, ge=-86_400_000, le=86_400_000)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    sample_rate_hz: int | None = Field(default=None, ge=1, le=384_000)
    channels: int | None = Field(default=None, ge=1, le=64)
    language: str | None = Field(default=None, min_length=1, max_length=64)
    subtitle_kind: SubtitleKind | None = None
    subtitle_provenance: SubtitleProvenance | None = None
    is_default: bool | None = None
    is_forced: bool | None = None

    @model_validator(mode="after")
    def fields_match_stream_type(self) -> Self:
        if (
            self.time_base.numerator > self.time_base.denominator
            or self.time_base.numerator * 1_000_000_000 < self.time_base.denominator
        ):
            raise ValueError("media stream time base exceeds safe limits")
        if self.stream_type is MediaStreamType.VIDEO:
            if self.width is None or self.height is None or self.frame_rate is None:
                raise ValueError("video stream requires dimensions and frame rate")
            if (
                self.frame_rate.numerator > self.frame_rate.denominator * 1000
                or self.frame_rate.numerator * 1000 < self.frame_rate.denominator
            ):
                raise ValueError("video stream frame rate exceeds safe limits")
            if any(
                value is not None
                for value in (
                    self.sample_rate_hz,
                    self.channels,
                    self.subtitle_kind,
                    self.subtitle_provenance,
                    self.is_default,
                    self.is_forced,
                )
            ):
                raise ValueError("video stream contains fields from another stream type")
        elif self.stream_type is MediaStreamType.AUDIO:
            if self.sample_rate_hz is None or self.channels is None:
                raise ValueError("audio stream requires sample rate and channels")
            if any(
                value is not None
                for value in (
                    self.width,
                    self.height,
                    self.subtitle_kind,
                    self.frame_rate,
                    self.subtitle_provenance,
                    self.is_default,
                    self.is_forced,
                )
            ):
                raise ValueError("audio stream contains fields from another stream type")
        else:
            if (
                self.subtitle_kind is None
                or self.subtitle_provenance is None
                or self.is_default is None
                or self.is_forced is None
            ):
                raise ValueError("subtitle stream requires provenance and disposition")
            if any(
                value is not None
                for value in (
                    self.width,
                    self.height,
                    self.sample_rate_hz,
                    self.channels,
                    self.frame_rate,
                )
            ):
                raise ValueError("subtitle stream contains fields from another stream type")
        return self


class MediaProbe(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    format_name: str = Field(min_length=1, max_length=256)
    duration_ms: int = Field(gt=0)
    start_time_ms: int = Field(default=0, ge=-86_400_000, le=86_400_000)
    streams: list[MediaStream] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def stream_indexes_are_canonical(self) -> Self:
        indexes = [stream.index for stream in self.streams]
        if indexes != sorted(set(indexes)):
            raise ValueError("media stream indexes must be sorted and unique")
        return self


class NormalizationConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    analysis_video_codec: Literal["libx264"] = "libx264"
    analysis_width: int = Field(default=640, ge=64, le=4096)
    analysis_height: int = Field(default=360, ge=64, le=4096)
    analysis_crf: int = Field(default=23, ge=0, le=51)
    speech_sample_rate_hz: int = Field(default=16000, ge=8000, le=96000)
    periodic_frame_interval_seconds: float = Field(default=30.0, gt=0, le=3600, allow_inf_nan=False)
    scene_change_threshold: float = Field(default=0.30, gt=0, lt=1, allow_inf_nan=False)
    frame_width: int = Field(default=320, ge=32, le=1920)
    frame_height: int = Field(default=180, ge=32, le=1080)
    perceptual_hash_distance: int = Field(default=4, ge=0, le=64)
    timestamp_merge_tolerance_seconds: float = Field(default=0.01, ge=0, le=1, allow_inf_nan=False)
    tool_timeout_seconds: float = Field(default=600.0, gt=0, le=86400, allow_inf_nan=False)
    stdout_limit_bytes: int = Field(default=4 * 1024 * 1024, ge=4096, le=64 * 1024 * 1024)
    stderr_limit_bytes: int = Field(default=4 * 1024 * 1024, ge=4096, le=64 * 1024 * 1024)
    duration_tolerance_ms: int = Field(default=1000, ge=0, le=10_000)
    maximum_duration_ms: int = Field(default=4 * 60 * 60 * 1000, gt=0, le=4 * 60 * 60 * 1000)
    maximum_source_width: int = Field(default=8192, ge=64, le=32768)
    maximum_source_height: int = Field(default=8192, ge=64, le=32768)
    maximum_source_pixels: int = Field(default=33_554_432, ge=4096, le=33_554_432)
    maximum_source_audio_sample_rate_hz: int = Field(default=192_000, ge=8000, le=384_000)
    maximum_source_audio_channels: int = Field(default=8, ge=1, le=64)
    maximum_decoded_video_pixel_slots_per_frame: int = Field(
        default=34_070_528,
        ge=4096,
        le=34_070_528,
        description="FFmpeg generic -max_pixels decoder slots per input frame",
    )
    maximum_decoded_audio_sample_slots_per_frame: int = Field(
        default=1_048_576,
        ge=1,
        le=1_048_576,
        description="FFmpeg generic -max_samples decoder slots per input audio frame",
    )
    maximum_streams: int = Field(default=128, ge=1, le=4096)
    maximum_frame_candidates: int = Field(default=512, ge=1, le=4096)
    maximum_output_bytes: int = Field(
        default=4 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=8 * 1024 * 1024 * 1024,
    )
    maximum_output_file_bytes: int = Field(
        default=1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=4 * 1024 * 1024 * 1024,
    )
    maximum_frame_output_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024 * 1024,
        le=64 * 1024 * 1024,
    )
    maximum_subtitle_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1024 * 1024,
        le=64 * 1024 * 1024,
    )

    @model_validator(mode="after")
    def output_budgets_are_coherent(self) -> Self:
        if any(
            limit > self.maximum_output_bytes
            for limit in (
                self.maximum_output_file_bytes,
                self.maximum_frame_output_bytes,
                self.maximum_subtitle_bytes,
            )
        ):
            raise ValueError("per-output limits must not exceed the aggregate output limit")
        maximum_speech_bytes = (
            self.maximum_duration_ms * self.speech_sample_rate_hz * 2 + 999
        ) // 1000 + 44
        if self.maximum_output_file_bytes < maximum_speech_bytes:
            raise ValueError("speech output limit cannot hold the configured maximum duration")
        return self


class SubtitleCue(StrictModel):
    cue_id: str = Field(min_length=1, max_length=128)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=65536)

    @model_validator(mode="after")
    def end_is_after_start(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("subtitle cue must end after it starts")
        return self


class NormalizedSubtitle(StrictModel):
    stream_index: int = Field(ge=0)
    codec_name: str = Field(min_length=1, max_length=128)
    language: str | None = Field(default=None, min_length=1, max_length=64)
    kind: SubtitleKind
    provenance: SubtitleProvenance
    is_default: bool
    is_forced: bool
    status: SubtitleStatus
    webvtt_artifact: ArtifactRef | None = None
    cues_artifact: ArtifactRef | None = None
    cue_count: int = Field(default=0, ge=0)
    warning_code: str | None = Field(default=None, min_length=1, max_length=128)
    # WebVTT transcode output deliberately does not claim source text/markup
    # preservation. Raw media bytes remain immutable in the raw artifact.
    raw_text_preserved: Literal[False] = Field(
        default=False,
        description="Always false: WebVTT normalization does not preserve source cue markup",
    )

    @model_validator(mode="after")
    def outputs_match_status(self) -> Self:
        if self.status is SubtitleStatus.NORMALIZED:
            if (
                self.kind is not SubtitleKind.TEXT
                or self.webvtt_artifact is None
                or self.cues_artifact is None
                or self.warning_code is not None
                or self.raw_text_preserved
                or self.codec_name.casefold() not in NORMALIZABLE_TEXT_SUBTITLE_CODEC_ALLOWLIST
            ):
                raise ValueError("normalized subtitle outputs are incomplete")
        elif (
            self.webvtt_artifact is not None
            or self.cues_artifact is not None
            or self.cue_count != 0
            or self.warning_code is None
            or self.raw_text_preserved
        ):
            raise ValueError("unsupported subtitle must only contain a warning")
        return self


class AnalysisProxy(StrictModel):
    artifact: ArtifactRef
    format_name: Literal["mov,mp4,m4a,3gp,3g2,mj2", "mov,mp4"]
    codec_name: Literal["h264"]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    duration_ms: int = Field(gt=0)


class SpeechAudio(StrictModel):
    artifact: ArtifactRef
    format_name: Literal["wav"]
    codec_name: Literal["pcm_s16le"]
    sample_rate_hz: int = Field(ge=1)
    channels: Literal[1]
    duration_ms: int = Field(gt=0)


class FrameRecord(StrictModel):
    frame_id: str = Field(pattern=r"^F[0-9]{6}$")
    timestamp_ms: int = Field(ge=0)
    artifact: ArtifactRef
    perceptual_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    reasons: list[FrameReason] = Field(min_length=1, max_length=2)
    candidate_timestamps_ms: list[int] = Field(min_length=1)

    @field_validator("reasons")
    @classmethod
    def reasons_are_canonical(cls, value: list[FrameReason]) -> list[FrameReason]:
        if value != sorted(set(value), key=lambda item: item.value):
            raise ValueError("frame reasons must be sorted and unique")
        return value

    @field_validator("candidate_timestamps_ms")
    @classmethod
    def candidates_are_canonical(cls, value: list[int]) -> list[int]:
        if any(item < 0 for item in value) or value != sorted(set(value)):
            raise ValueError("candidate timestamps must be sorted, unique, and non-negative")
        return value


class FrameIndex(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    duration_ms: int = Field(gt=0)
    records: list[FrameRecord] = Field(default_factory=list)
    candidate_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)

    @model_validator(mode="after")
    def records_are_ordered_and_bounded(self) -> Self:
        ids = [record.frame_id for record in self.records]
        timestamps = [record.timestamp_ms for record in self.records]
        if ids != [f"F{index:06d}" for index in range(1, len(ids) + 1)]:
            raise ValueError("frame IDs must be contiguous")
        if timestamps != sorted(timestamps):
            raise ValueError("frame timestamps must be sorted")
        if any(record.timestamp_ms > self.duration_ms for record in self.records):
            raise ValueError("frame record timestamp is out of range")
        if any(
            candidate > self.duration_ms
            for record in self.records
            for candidate in record.candidate_timestamps_ms
        ):
            raise ValueError("frame timestamp exceeds media duration")
        if self.candidate_count != len(self.records) + self.duplicate_count:
            raise ValueError("frame candidate accounting is inconsistent")
        return self


class MediaManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    normalization_fingerprint: Sha256
    implementation_version: str = Field(min_length=1, max_length=128)
    configuration: NormalizationConfig
    primary_input_kind: PrimaryInputKind = PrimaryInputKind.VIDEO
    ffmpeg: ToolIdentity | None = None
    ffprobe: ToolIdentity | None = None
    raw_artifact: ArtifactRef
    probe: MediaProbe | None = None
    analysis_proxy: AnalysisProxy | None = None
    speech_audio: SpeechAudio | None = None
    subtitles: list[NormalizedSubtitle] = Field(default_factory=list)
    frame_index_artifact: ArtifactRef | None = None
    frame_index: FrameIndex | None = None
    warnings: list[str] = Field(default_factory=list, max_length=128)
    started_at: UtcDateTime
    created_at: UtcDateTime

    @model_validator(mode="after")
    def bundle_is_internally_consistent(self) -> Self:
        if self.created_at < self.started_at:
            raise ValueError("media manifest timestamps are inconsistent")
        if self.primary_input_kind is PrimaryInputKind.TRANSCRIPT:
            if (
                any(
                    item is not None
                    for item in (
                        self.ffmpeg,
                        self.ffprobe,
                        self.probe,
                        self.analysis_proxy,
                        self.speech_audio,
                        self.frame_index_artifact,
                        self.frame_index,
                    )
                )
                or self.subtitles
            ):
                raise ValueError("transcript primary input must not claim normalized media")
            if self.warnings != ["TRANSCRIPT_PRIMARY"]:
                raise ValueError("transcript primary input requires its canonical warning")
            return self
        if self.ffmpeg is None or self.ffprobe is None or self.probe is None:
            raise ValueError("media primary input requires FFmpeg, ffprobe, and a media probe")
        has_audio = any(
            stream.stream_type is MediaStreamType.AUDIO for stream in self.probe.streams
        )
        if has_audio != (self.speech_audio is not None):
            raise ValueError("speech audio output does not match probed streams")
        if not has_audio and "AUDIO_STREAM_UNAVAILABLE" not in self.warnings:
            raise ValueError("missing audio stream warning is required")
        subtitle_indexes = [subtitle.stream_index for subtitle in self.subtitles]
        if subtitle_indexes != sorted(set(subtitle_indexes)):
            raise ValueError("normalized subtitles must be sorted and unique")
        if self.warnings != sorted(set(self.warnings)):
            raise ValueError("media warnings must be sorted and unique")
        config = self.configuration
        if self.primary_input_kind is PrimaryInputKind.AUDIO:
            if (
                any(stream.stream_type is MediaStreamType.VIDEO for stream in self.probe.streams)
                or self.analysis_proxy is not None
                or self.frame_index_artifact is not None
                or self.frame_index is not None
                or self.subtitles
                or self.speech_audio is None
            ):
                raise ValueError("audio primary input must not create video outputs")
            if (
                self.probe.duration_ms > config.maximum_duration_ms
                or len(self.probe.streams) > config.maximum_streams
                or self.speech_audio.sample_rate_hz != config.speech_sample_rate_hz
                or abs(self.speech_audio.duration_ms - self.probe.duration_ms)
                > config.duration_tolerance_ms
            ):
                raise ValueError("audio outputs exceed or mismatch normalization configuration")
            return self
        if not any(stream.stream_type is MediaStreamType.VIDEO for stream in self.probe.streams):
            raise ValueError("video primary input requires a video stream")
        if (
            self.analysis_proxy is None
            or self.frame_index_artifact is None
            or self.frame_index is None
        ):
            raise ValueError("video primary input requires proxy and frame outputs")
        if (
            self.probe.duration_ms > config.maximum_duration_ms
            or len(self.probe.streams) > config.maximum_streams
            or self.frame_index.duration_ms != self.probe.duration_ms
            or self.frame_index.candidate_count > config.maximum_frame_candidates
            or self.analysis_proxy.width != config.analysis_width
            or self.analysis_proxy.height != config.analysis_height
            or abs(self.analysis_proxy.duration_ms - self.probe.duration_ms)
            > config.duration_tolerance_ms
        ):
            raise ValueError("media outputs exceed or mismatch normalization configuration")
        if self.speech_audio is not None and (
            self.speech_audio.sample_rate_hz != config.speech_sample_rate_hz
            or abs(self.speech_audio.duration_ms - self.probe.duration_ms)
            > config.duration_tolerance_ms
        ):
            raise ValueError("speech audio does not match normalization configuration")
        video_streams = [
            stream for stream in self.probe.streams if stream.stream_type is MediaStreamType.VIDEO
        ]
        if any(
            stream.width is None
            or stream.height is None
            or stream.width > config.maximum_source_width
            or stream.height > config.maximum_source_height
            or stream.width * stream.height > config.maximum_source_pixels
            for stream in video_streams
        ):
            raise ValueError("probed source dimensions exceed normalization configuration")
        audio_streams = [
            stream for stream in self.probe.streams if stream.stream_type is MediaStreamType.AUDIO
        ]
        if any(
            stream.sample_rate_hz is None
            or stream.channels is None
            or stream.sample_rate_hz > config.maximum_source_audio_sample_rate_hz
            or stream.channels > config.maximum_source_audio_channels
            for stream in audio_streams
        ):
            raise ValueError("probed source audio exceeds normalization configuration")
        subtitle_streams = {
            stream.index: stream
            for stream in self.probe.streams
            if stream.stream_type is MediaStreamType.SUBTITLE
        }
        if set(subtitle_streams) != set(subtitle_indexes):
            raise ValueError("subtitle outputs do not cover probed subtitle streams")
        if any(
            subtitle.codec_name != subtitle_streams[subtitle.stream_index].codec_name
            or subtitle.language != subtitle_streams[subtitle.stream_index].language
            or subtitle.kind is not subtitle_streams[subtitle.stream_index].subtitle_kind
            or subtitle.provenance
            is not subtitle_streams[subtitle.stream_index].subtitle_provenance
            or subtitle.is_default is not subtitle_streams[subtitle.stream_index].is_default
            or subtitle.is_forced is not subtitle_streams[subtitle.stream_index].is_forced
            for subtitle in self.subtitles
        ):
            raise ValueError("subtitle output provenance does not match probe metadata")
        artifacts = [
            self.analysis_proxy.artifact,
            self.frame_index_artifact,
            *(record.artifact for record in self.frame_index.records),
            *([self.speech_audio.artifact] if self.speech_audio is not None else []),
            *(
                artifact
                for subtitle in self.subtitles
                for artifact in (subtitle.webvtt_artifact, subtitle.cues_artifact)
                if artifact is not None
            ),
        ]
        subtitle_artifacts = [
            artifact
            for subtitle in self.subtitles
            for artifact in (subtitle.webvtt_artifact, subtitle.cues_artifact)
            if artifact is not None
        ]
        if any(
            artifact.size_bytes > config.maximum_subtitle_bytes for artifact in subtitle_artifacts
        ):
            raise ValueError("normalized subtitle exceeds configured size")
        if sum(artifact.size_bytes for artifact in artifacts) > config.maximum_output_bytes:
            raise ValueError("normalized output bundle exceeds configured size")
        return self
