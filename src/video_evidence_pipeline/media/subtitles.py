"""Bounded embedded subtitle normalization to WebVTT plus normalized cue JSONL."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from video_evidence_pipeline.domain.media import (
    NORMALIZABLE_TEXT_SUBTITLE_CODEC_ALLOWLIST,
    MediaProbe,
    MediaStream,
    MediaStreamType,
    NormalizationConfig,
    SubtitleCue,
    SubtitleKind,
    SubtitleStatus,
)
from video_evidence_pipeline.domain.serialization import canonical_json_bytes
from video_evidence_pipeline.errors import integrity_error
from video_evidence_pipeline.media.budget import StagingBudget
from video_evidence_pipeline.media.commands import ffmpeg_local_input
from video_evidence_pipeline.media.runner import FFmpegRunner
from video_evidence_pipeline.media.staging import prepare_private_output
from video_evidence_pipeline.storage.jsonl import append_jsonl
from video_evidence_pipeline.storage.permissions import enforce_private_file

_TIMING = re.compile(
    r"^(?P<start>(?:[0-9]{2}:)?[0-9]{2}:[0-9]{2}\.[0-9]{3})\s+-->\s+"
    r"(?P<end>(?:[0-9]{2}:)?[0-9]{2}:[0-9]{2}\.[0-9]{3})(?:\s+.*)?$"
)


@dataclass(frozen=True, slots=True)
class StagedSubtitle:
    stream: MediaStream
    status: SubtitleStatus
    webvtt_path: Path | None
    cues_path: Path | None
    cues: tuple[SubtitleCue, ...]
    warning_code: str | None


def _timestamp_ms(value: str) -> int:
    pieces = value.split(":")
    if len(pieces) == 2:
        hours = 0
        minutes_text, seconds_text = pieces
    elif len(pieces) == 3:
        hours_text, minutes_text, seconds_text = pieces
        hours = int(hours_text)
    else:
        raise ValueError
    seconds, milliseconds = seconds_text.split(".")
    minutes = int(minutes_text)
    second_value = int(seconds)
    millisecond_value = int(milliseconds)
    if minutes >= 60 or second_value >= 60 or millisecond_value >= 1000:
        raise ValueError
    return ((hours * 60 + minutes) * 60 + second_value) * 1000 + millisecond_value


def parse_webvtt(
    payload: bytes,
    *,
    stream_index: int,
    duration_ms: int,
    tolerance_ms: int = 0,
) -> list[SubtitleCue]:
    try:
        text = payload.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise integrity_error("SUBTITLE_VTT_INVALID", "normalized subtitle is not UTF-8") from None
    if not text.startswith("WEBVTT"):
        raise integrity_error("SUBTITLE_VTT_INVALID", "normalized subtitle header is invalid")
    blocks = re.split(r"\n[ \t]*\n", text.rstrip("\n"))
    cues: list[SubtitleCue] = []
    for block in blocks[1:]:
        lines = block.split("\n")
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_index = 0 if "-->" in lines[0] else 1
        if timing_index >= len(lines):
            raise integrity_error("SUBTITLE_VTT_INVALID", "subtitle cue timing is missing")
        match = _TIMING.fullmatch(lines[timing_index])
        if match is None or timing_index + 1 >= len(lines):
            raise integrity_error("SUBTITLE_VTT_INVALID", "subtitle cue is malformed")
        try:
            start_ms = _timestamp_ms(match.group("start"))
            end_ms = _timestamp_ms(match.group("end"))
        except (ValueError, OverflowError):
            raise integrity_error("SUBTITLE_VTT_INVALID", "subtitle timestamp is invalid") from None
        raw_text = "\n".join(lines[timing_index + 1 :])
        if not raw_text or end_ms <= start_ms or end_ms > duration_ms + tolerance_ms:
            raise integrity_error(
                "SUBTITLE_TIMESTAMP_INVALID", "subtitle timestamp is out of range"
            )
        try:
            cue = SubtitleCue(
                cue_id=f"S{stream_index:04d}-C{len(cues) + 1:06d}",
                start_ms=start_ms,
                end_ms=end_ms,
                text=raw_text,
            )
        except ValidationError:
            raise integrity_error("SUBTITLE_VTT_INVALID", "subtitle cue is invalid") from None
        cues.append(cue)
    return cues


def _bounded_subtitle_file(path: Path, *, maximum_bytes: int) -> int:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > maximum_bytes
        ):
            raise integrity_error("NORMALIZED_OUTPUT_TOO_LARGE", "subtitle output is too large")
        return metadata.st_size
    except OSError:
        raise integrity_error("SUBTITLE_OUTPUT_MISSING", "subtitle output is unavailable") from None


def _read_bounded_subtitle(path: Path, *, maximum_bytes: int) -> bytes:
    expected_size = _bounded_subtitle_file(path, maximum_bytes=maximum_bytes)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise integrity_error("SUBTITLE_OUTPUT_MISSING", "subtitle output is unavailable") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise integrity_error("NORMALIZED_OUTPUT_TOO_LARGE", "subtitle output is too large")
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes - len(payload) + 1))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise integrity_error("SUBTITLE_OUTPUT_MISSING", "subtitle output is unavailable") from None
    finally:
        os.close(descriptor)
    if (
        len(payload) != expected_size
        or len(payload) > maximum_bytes
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise integrity_error("NORMALIZED_OUTPUT_TOO_LARGE", "subtitle output is too large")
    return bytes(payload)


def _read_bounded_cue_jsonl(path: Path, *, maximum_bytes: int) -> tuple[SubtitleCue, ...]:
    payload = _read_bounded_subtitle(path, maximum_bytes=maximum_bytes)
    if not payload or not payload.endswith(b"\n"):
        raise integrity_error("SUBTITLE_CUES_INVALID", "subtitle cue JSONL is invalid")
    try:
        return tuple(
            SubtitleCue.model_validate(json.loads(line.decode("utf-8")))
            for line in payload.splitlines()
        )
    except (UnicodeError, json.JSONDecodeError, ValidationError):
        raise integrity_error("SUBTITLE_CUES_INVALID", "subtitle cue JSONL is invalid") from None


def verify_cue_jsonl(
    path: Path,
    expected: tuple[SubtitleCue, ...] | None = None,
    *,
    maximum_bytes: int,
) -> tuple[SubtitleCue, ...]:
    actual = _read_bounded_cue_jsonl(path, maximum_bytes=maximum_bytes)
    if expected is not None and actual != expected:
        raise integrity_error("SUBTITLE_CUES_INVALID", "subtitle cue JSONL text is inconsistent")
    return actual


def extract_subtitles(
    runner: FFmpegRunner,
    source: Path,
    probe: MediaProbe,
    config: NormalizationConfig,
    staging_directory: Path,
    budget: StagingBudget,
) -> list[StagedSubtitle]:
    results: list[StagedSubtitle] = []
    for stream in probe.streams:
        if stream.stream_type is not MediaStreamType.SUBTITLE:
            continue
        if stream.subtitle_kind is not SubtitleKind.TEXT or (
            stream.codec_name not in NORMALIZABLE_TEXT_SUBTITLE_CODEC_ALLOWLIST
        ):
            warning = (
                "SUBTITLE_BITMAP_UNSUPPORTED"
                if stream.subtitle_kind is SubtitleKind.BITMAP
                else (
                    "SUBTITLE_NORMALIZATION_UNSUPPORTED"
                    if stream.subtitle_kind is SubtitleKind.TEXT
                    else "SUBTITLE_CODEC_UNSUPPORTED"
                )
            )
            results.append(
                StagedSubtitle(
                    stream=stream,
                    status=SubtitleStatus.UNSUPPORTED,
                    webvtt_path=None,
                    cues_path=None,
                    cues=(),
                    warning_code=warning,
                )
            )
            continue
        webvtt_path = staging_directory / f"subtitle-{stream.index:04d}.vtt"
        cues_path = staging_directory / f"subtitle-{stream.index:04d}.jsonl"
        budget.require_capacity(config.maximum_subtitle_bytes)
        prepare_private_output(webvtt_path)
        runner.run_checked(
            "ffmpeg",
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                *ffmpeg_local_input(
                    source,
                    config.maximum_streams,
                    config.maximum_decoded_video_pixel_slots_per_frame,
                    config.maximum_decoded_audio_sample_slots_per_frame,
                ),
                "-map",
                f"0:{stream.index}",
                "-map_metadata",
                "-1",
                "-c:s",
                "webvtt",
                "-f",
                "webvtt",
                "-fs",
                str(config.maximum_subtitle_bytes),
                "-y",
                str(webvtt_path.absolute()),
            ],
            timeout_seconds=config.tool_timeout_seconds,
            stdout_limit_bytes=config.stdout_limit_bytes,
            stderr_limit_bytes=config.stderr_limit_bytes,
        )
        try:
            enforce_private_file(webvtt_path)
            budget.record_file(webvtt_path, maximum_file_bytes=config.maximum_subtitle_bytes)
            cues = tuple(
                parse_webvtt(
                    _read_bounded_subtitle(
                        webvtt_path,
                        maximum_bytes=config.maximum_subtitle_bytes,
                    ),
                    stream_index=stream.index,
                    duration_ms=probe.duration_ms,
                    tolerance_ms=config.duration_tolerance_ms,
                )
            )
        except OSError:
            raise integrity_error(
                "SUBTITLE_OUTPUT_MISSING", "subtitle output is unavailable"
            ) from None
        if not cues:
            webvtt_path.unlink(missing_ok=True)
            results.append(
                StagedSubtitle(
                    stream=stream,
                    status=SubtitleStatus.UNSUPPORTED,
                    webvtt_path=None,
                    cues_path=None,
                    cues=(),
                    warning_code="SUBTITLE_TEXT_EMPTY",
                )
            )
            continue
        prepare_private_output(cues_path)
        budget.record_file(cues_path, maximum_file_bytes=config.maximum_subtitle_bytes)
        for cue in cues:
            serialized = canonical_json_bytes(cue) + b"\n"
            budget.require_file_growth(
                cues_path,
                additional_bytes=len(serialized),
                maximum_file_bytes=config.maximum_subtitle_bytes,
            )
            append_jsonl(cues_path, cue)
            budget.record_file(cues_path, maximum_file_bytes=config.maximum_subtitle_bytes)
        verify_cue_jsonl(
            cues_path,
            cues,
            maximum_bytes=config.maximum_subtitle_bytes,
        )
        results.append(
            StagedSubtitle(
                stream=stream,
                status=SubtitleStatus.NORMALIZED,
                webvtt_path=webvtt_path,
                cues_path=cues_path,
                cues=cues,
                warning_code=None,
            )
        )
    return results
