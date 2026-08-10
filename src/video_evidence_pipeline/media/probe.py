"""Fail-closed ffprobe parsing for local immutable media."""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from math import gcd
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from video_evidence_pipeline.domain.media import (
    MediaProbe,
    MediaRational,
    MediaStream,
    MediaStreamType,
    NormalizationConfig,
    SubtitleKind,
    SubtitleProvenance,
)
from video_evidence_pipeline.errors import integrity_error
from video_evidence_pipeline.media.commands import ffprobe_local_input
from video_evidence_pipeline.media.runner import FFmpegRunner

_PLAYLIST_OR_EXTERNAL_FORMATS = {
    "concat",
    "dash",
    "hls",
    "image2",
    "image2pipe",
    "live_flv",
    "m3u8",
    "rtsp",
    "sap",
    "sdp",
    "smoothstreaming",
}
_TEXT_SUBTITLE_CODECS = {
    "ass",
    "eia_608",
    "eia_708",
    "microdvd",
    "mov_text",
    "sami",
    "srt",
    "ssa",
    "subrip",
    "text",
    "ttml",
    "webvtt",
}
_BITMAP_SUBTITLE_CODECS = {
    "dvb_subtitle",
    "dvd_subtitle",
    "hdmv_pgs_subtitle",
    "pgssub",
    "xsub",
}


def _integer(value: Any, *, minimum: int, code: str) -> int:
    if isinstance(value, bool):
        raise integrity_error(code, "ffprobe returned invalid integer metadata")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise integrity_error(code, "ffprobe returned invalid integer metadata") from None
    if str(parsed) != str(value).strip() and not isinstance(value, int):
        raise integrity_error(code, "ffprobe returned non-canonical integer metadata")
    if parsed < minimum:
        raise integrity_error(code, "ffprobe returned out-of-range integer metadata")
    return parsed


def _milliseconds(value: Any, *, required: bool, code: str) -> int | None:
    if value is None or (isinstance(value, str) and value in {"", "N/A"}):
        if required:
            raise integrity_error(code, "ffprobe did not provide required duration metadata")
        return None
    try:
        text = str(value)
        if len(text) > 128:
            raise ValueError
        decimal = Decimal(text)
        if not decimal.is_finite() or decimal.adjusted() > 9:
            raise ValueError
        milliseconds = int((decimal * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError, OverflowError):
        raise integrity_error(code, "ffprobe returned invalid timestamp metadata") from None
    if required and milliseconds <= 0:
        raise integrity_error(code, "ffprobe returned non-positive duration metadata")
    return milliseconds


def _mapping(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise integrity_error(code, "ffprobe returned malformed object metadata")
    return value


def _text(value: Any, *, code: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise integrity_error(code, "ffprobe returned invalid text metadata")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise integrity_error(code, "ffprobe returned invalid text metadata")
    return normalized


def _disposition(value: Any, key: str) -> bool:
    mapping = _mapping({} if value is None else value, code="FFPROBE_STREAM_INVALID")
    raw = mapping.get(key, 0)
    if not isinstance(raw, (bool, int)) or int(raw) not in {0, 1}:
        raise integrity_error("FFPROBE_STREAM_INVALID", "subtitle disposition is invalid")
    return bool(raw)


def _subtitle_kind(codec_name: str) -> SubtitleKind:
    if codec_name in _TEXT_SUBTITLE_CODECS:
        return SubtitleKind.TEXT
    if codec_name in _BITMAP_SUBTITLE_CODECS:
        return SubtitleKind.BITMAP
    return SubtitleKind.UNKNOWN


def _rational(value: Any, *, code: str) -> MediaRational:
    if not isinstance(value, str) or value.count("/") != 1:
        raise integrity_error(code, "ffprobe returned invalid rational metadata")
    numerator_text, denominator_text = value.split("/")
    try:
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (ValueError, OverflowError):
        raise integrity_error(code, "ffprobe returned invalid rational metadata") from None
    if (
        numerator <= 0
        or denominator <= 0
        or numerator > 2_147_483_647
        or denominator > 2_147_483_647
    ):
        raise integrity_error(code, "ffprobe returned out-of-range rational metadata")
    divisor = gcd(numerator, denominator)
    return MediaRational(numerator=numerator // divisor, denominator=denominator // divisor)


def _parse_stream(raw: Any, config: NormalizationConfig) -> MediaStream | None:
    stream = _mapping(raw, code="FFPROBE_STREAM_INVALID")
    stream_type_raw = stream.get("codec_type")
    if stream_type_raw not in {item.value for item in MediaStreamType}:
        return None
    stream_type = MediaStreamType(stream_type_raw)
    index = _integer(stream.get("index"), minimum=0, code="FFPROBE_STREAM_INVALID")
    codec_name = _text(stream.get("codec_name"), code="FFPROBE_STREAM_INVALID")
    duration_ms = _milliseconds(
        stream.get("duration"), required=False, code="FFPROBE_STREAM_INVALID"
    )
    start_time_ms = _milliseconds(
        stream.get("start_time"), required=False, code="FFPROBE_STREAM_INVALID"
    )
    tags_value = stream.get("tags")
    tags = _mapping(
        {} if tags_value is None else tags_value,
        code="FFPROBE_STREAM_INVALID",
    )
    language_value = tags.get("language")
    language = (
        None
        if language_value is None or language_value == ""
        else _text(language_value, code="FFPROBE_STREAM_INVALID", maximum=64)
    )
    common: dict[str, Any] = {
        "index": index,
        "stream_type": stream_type,
        "codec_name": codec_name,
        "time_base": _rational(stream.get("time_base"), code="FFPROBE_STREAM_INVALID"),
        "duration_ms": duration_ms,
        "start_time_ms": start_time_ms,
        "language": language,
    }
    if stream_type is MediaStreamType.VIDEO:
        width = _integer(stream.get("width"), minimum=1, code="MEDIA_DIMENSIONS_INVALID")
        height = _integer(stream.get("height"), minimum=1, code="MEDIA_DIMENSIONS_INVALID")
        if (
            width > config.maximum_source_width
            or height > config.maximum_source_height
            or width * height > config.maximum_source_pixels
        ):
            raise integrity_error(
                "MEDIA_DIMENSIONS_EXCEEDED", "source dimensions exceed safe limits"
            )
        return MediaStream(
            **common,
            width=width,
            height=height,
            frame_rate=_rational(stream.get("avg_frame_rate"), code="FFPROBE_STREAM_INVALID"),
        )
    if stream_type is MediaStreamType.AUDIO:
        sample_rate_hz = _integer(
            stream.get("sample_rate"), minimum=1, code="FFPROBE_STREAM_INVALID"
        )
        channels = _integer(stream.get("channels"), minimum=1, code="FFPROBE_STREAM_INVALID")
        if (
            sample_rate_hz > config.maximum_source_audio_sample_rate_hz
            or channels > config.maximum_source_audio_channels
        ):
            raise integrity_error("MEDIA_AUDIO_LIMIT_EXCEEDED", "source audio exceeds safe limits")
        return MediaStream(
            **common,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
    return MediaStream(
        **common,
        subtitle_kind=_subtitle_kind(codec_name),
        subtitle_provenance=SubtitleProvenance.EMBEDDED,
        is_default=_disposition(stream.get("disposition"), "default"),
        is_forced=_disposition(stream.get("disposition"), "forced"),
    )


def parse_probe_payload(payload: bytes, config: NormalizationConfig) -> MediaProbe:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise integrity_error("FFPROBE_OUTPUT_INVALID", "ffprobe output is malformed") from None
    root = _mapping(decoded, code="FFPROBE_OUTPUT_INVALID")
    if set(root) - {"format", "streams", "chapters", "programs", "stream_groups"}:
        raise integrity_error("FFPROBE_OUTPUT_INVALID", "ffprobe output contains unexpected fields")
    raw_streams = root.get("streams")
    if not isinstance(raw_streams, list):
        raise integrity_error("FFPROBE_OUTPUT_INVALID", "ffprobe stream list is missing")
    if not raw_streams or len(raw_streams) > config.maximum_streams:
        raise integrity_error("MEDIA_STREAM_LIMIT_EXCEEDED", "source stream count is invalid")
    format_data = _mapping(root.get("format"), code="FFPROBE_OUTPUT_INVALID")
    format_name = _text(format_data.get("format_name"), code="FFPROBE_OUTPUT_INVALID", maximum=256)
    format_tokens = {item.strip().lower() for item in format_name.split(",")}
    if format_tokens & _PLAYLIST_OR_EXTERNAL_FORMATS:
        raise integrity_error(
            "MEDIA_EXTERNAL_REFERENCE_UNSUPPORTED",
            "playlist and external-reference media formats are unsupported",
        )
    duration_ms = _milliseconds(
        format_data.get("duration"), required=True, code="MEDIA_DURATION_INVALID"
    )
    if duration_ms is None:
        raise integrity_error("MEDIA_DURATION_INVALID", "media duration is unavailable")
    if duration_ms > config.maximum_duration_ms:
        raise integrity_error("MEDIA_DURATION_EXCEEDED", "source duration exceeds safe limits")
    start_time_ms = _milliseconds(
        format_data.get("start_time", "0"), required=False, code="MEDIA_DURATION_INVALID"
    )
    try:
        parsed_streams = [
            parsed
            for raw_stream in raw_streams
            if (parsed := _parse_stream(raw_stream, config)) is not None
        ]
    except ValidationError:
        raise integrity_error(
            "FFPROBE_STREAM_INVALID", "ffprobe stream metadata is invalid"
        ) from None
    parsed_streams.sort(key=lambda item: item.index)
    try:
        probe = MediaProbe(
            format_name=format_name,
            duration_ms=duration_ms,
            start_time_ms=start_time_ms or 0,
            streams=parsed_streams,
        )
    except ValueError:
        raise integrity_error(
            "FFPROBE_STREAM_INVALID", "ffprobe stream metadata is invalid"
        ) from None
    if any(
        stream.duration_ms is not None
        and stream.duration_ms > duration_ms + max(1000, math.ceil(duration_ms / 100))
        for stream in probe.streams
    ):
        raise integrity_error(
            "MEDIA_DURATION_MISMATCH", "stream duration exceeds container duration"
        )
    return probe


def probe_media(
    runner: FFmpegRunner,
    source: Path,
    config: NormalizationConfig,
) -> MediaProbe:
    result = runner.run_checked(
        "ffprobe",
        [
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            *ffprobe_local_input(
                source,
                config.maximum_streams,
                config.maximum_decoded_video_pixel_slots_per_frame,
                config.maximum_decoded_audio_sample_slots_per_frame,
            ),
        ],
        timeout_seconds=config.tool_timeout_seconds,
        stdout_limit_bytes=config.stdout_limit_bytes,
        stderr_limit_bytes=config.stderr_limit_bytes,
        require_complete_stdout=True,
    )
    return parse_probe_payload(result.stdout, config)
