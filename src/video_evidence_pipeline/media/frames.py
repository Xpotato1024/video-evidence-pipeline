"""Periodic plus FFmpeg scene-change frame selection with pHash deduplication."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from video_evidence_pipeline.domain.media import FrameReason, MediaProbe, NormalizationConfig
from video_evidence_pipeline.errors import VepError, integrity_error, io_error
from video_evidence_pipeline.media.budget import StagingBudget
from video_evidence_pipeline.media.commands import ffmpeg_local_input
from video_evidence_pipeline.media.perceptual import (
    perceptual_hash_distance,
    perceptual_hash_png,
)
from video_evidence_pipeline.media.runner import FFmpegRunner
from video_evidence_pipeline.media.staging import prepare_private_output
from video_evidence_pipeline.storage.permissions import enforce_private_file

_SHOWINFO_TIMESTAMP = re.compile(r"\bpts_time:(?P<timestamp>-?[0-9]+(?:\.[0-9]+)?)")


@dataclass(frozen=True, slots=True)
class FrameCandidate:
    timestamp_ms: int
    reasons: tuple[FrameReason, ...]
    source_timestamps_ms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StagedFrame:
    path: Path
    timestamp_ms: int
    perceptual_hash: str
    reasons: tuple[FrameReason, ...]
    candidate_timestamps_ms: tuple[int, ...]


def _parse_scene_timestamps(payload: bytes, *, duration_ms: int) -> list[int]:
    timestamps: set[int] = set()
    text = payload.decode("utf-8", errors="replace")
    for match in _SHOWINFO_TIMESTAMP.finditer(text):
        try:
            timestamp = Decimal(match.group("timestamp"))
            if not timestamp.is_finite() or timestamp.adjusted() > 9:
                raise ValueError
            milliseconds = int((timestamp * 1000).to_integral_value(rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError, OverflowError):
            raise integrity_error("SCENE_TIMESTAMP_INVALID", "scene timestamp is invalid") from None
        if milliseconds < 0 or milliseconds > duration_ms:
            raise integrity_error("SCENE_TIMESTAMP_INVALID", "scene timestamp is out of range")
        timestamps.add(milliseconds)
    return sorted(timestamps)


def _parse_extracted_frame_timestamp(
    payload: bytes,
    *,
    duration_ms: int,
    start_time_ms: int,
) -> int:
    observed: set[int] = set()
    text = payload.decode("utf-8", errors="replace")
    for match in _SHOWINFO_TIMESTAMP.finditer(text):
        try:
            timestamp = Decimal(match.group("timestamp"))
            if not timestamp.is_finite() or timestamp.adjusted() > 9:
                raise ValueError
            observed.add(int((timestamp * 1000).to_integral_value(rounding=ROUND_HALF_UP)))
        except (InvalidOperation, ValueError, OverflowError):
            raise integrity_error(
                "FRAME_TIMESTAMP_INVALID", "extracted frame timestamp is invalid"
            ) from None
    if len(observed) != 1:
        raise integrity_error("FRAME_TIMESTAMP_INVALID", "extracted frame timestamp is unavailable")
    timestamp_ms = next(iter(observed)) - start_time_ms
    if timestamp_ms < 0 or timestamp_ms > duration_ms:
        raise integrity_error(
            "FRAME_TIMESTAMP_INVALID", "extracted frame timestamp is out of range"
        )
    return timestamp_ms


def scene_change_candidates(
    runner: FFmpegRunner,
    source: Path,
    probe: MediaProbe,
    config: NormalizationConfig,
) -> list[int]:
    try:
        result = runner.run_checked(
            "ffmpeg",
            [
                "-hide_banner",
                "-loglevel",
                "info",
                "-nostdin",
                "-nostats",
                *ffmpeg_local_input(
                    source,
                    config.maximum_streams,
                    config.maximum_decoded_video_pixel_slots_per_frame,
                    config.maximum_decoded_audio_sample_slots_per_frame,
                ),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"select=gt(scene\\,{config.scene_change_threshold:.6f}),showinfo",
                "-fps_mode",
                "vfr",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=config.tool_timeout_seconds,
            stdout_limit_bytes=config.stdout_limit_bytes,
            stderr_limit_bytes=config.stderr_limit_bytes,
            require_complete_stderr=True,
        )
    except VepError:
        raise io_error(
            "SCENE_DETECTION_FAILED",
            "FFmpeg scene detection failed; periodic fallback was not used",
        ) from None
    return _parse_scene_timestamps(result.stderr, duration_ms=probe.duration_ms)


def build_candidates(
    duration_ms: int,
    scene_timestamps_ms: list[int],
    config: NormalizationConfig,
) -> list[FrameCandidate]:
    interval_ms = max(1, round(config.periodic_frame_interval_seconds * 1000))
    periodic_count = (duration_ms + interval_ms - 1) // interval_ms
    if (
        periodic_count > config.maximum_frame_candidates
        or len(scene_timestamps_ms) > config.maximum_frame_candidates
    ):
        raise integrity_error(
            "FRAME_CANDIDATE_LIMIT_EXCEEDED", "frame candidate count exceeds safe limits"
        )
    items = [(timestamp, FrameReason.PERIODIC) for timestamp in range(0, duration_ms, interval_ms)]
    items.extend((timestamp, FrameReason.SCENE_CHANGE) for timestamp in scene_timestamps_ms)
    items.sort(key=lambda item: (item[0], item[1].value))
    tolerance_ms = round(config.timestamp_merge_tolerance_seconds * 1000)
    groups: list[list[tuple[int, FrameReason]]] = []
    for item in items:
        if groups and item[0] - groups[-1][-1][0] <= tolerance_ms:
            groups[-1].append(item)
        else:
            groups.append([item])
    if len(groups) > config.maximum_frame_candidates:
        raise integrity_error(
            "FRAME_CANDIDATE_LIMIT_EXCEEDED", "frame candidate count exceeds safe limits"
        )
    return [
        FrameCandidate(
            timestamp_ms=min(timestamp for timestamp, _reason in group),
            reasons=tuple(
                sorted({reason for _timestamp, reason in group}, key=lambda item: item.value)
            ),
            source_timestamps_ms=tuple(sorted({timestamp for timestamp, _reason in group})),
        )
        for group in groups
    ]


def _merge_frame(left: StagedFrame, right: StagedFrame) -> StagedFrame:
    return StagedFrame(
        path=left.path,
        timestamp_ms=left.timestamp_ms,
        perceptual_hash=left.perceptual_hash,
        reasons=tuple(sorted({*left.reasons, *right.reasons}, key=lambda item: item.value)),
        candidate_timestamps_ms=tuple(
            sorted({*left.candidate_timestamps_ms, *right.candidate_timestamps_ms})
        ),
    )


def merge_near_duplicate(
    retained: list[StagedFrame],
    candidate: StagedFrame,
    *,
    maximum_distance: int,
) -> bool:
    duplicate_index = next(
        (
            retained_index
            for retained_index, existing in enumerate(retained)
            if perceptual_hash_distance(existing.perceptual_hash, candidate.perceptual_hash)
            <= maximum_distance
        ),
        None,
    )
    if duplicate_index is None:
        retained.append(candidate)
        return False
    retained[duplicate_index] = _merge_frame(retained[duplicate_index], candidate)
    return True


def extract_frames(
    runner: FFmpegRunner,
    source: Path,
    probe: MediaProbe,
    config: NormalizationConfig,
    staging_directory: Path,
    budget: StagingBudget,
) -> tuple[list[StagedFrame], int]:
    candidates = build_candidates(
        probe.duration_ms,
        scene_change_candidates(runner, source, probe, config),
        config,
    )
    retained: list[StagedFrame] = []
    duplicate_count = 0
    for index, candidate in enumerate(candidates, start=1):
        path = staging_directory / f"candidate-{index:06d}.png"
        budget.require_capacity(config.maximum_frame_output_bytes)
        prepare_private_output(path)
        result = runner.run_checked(
            "ffmpeg",
            [
                "-hide_banner",
                "-loglevel",
                "info",
                "-nostdin",
                "-nostats",
                # Keep source PTS so the value logged by showinfo identifies
                # the decoded frame, rather than the requested seek position.
                "-copyts",
                "-ss",
                f"{candidate.timestamp_ms / 1000:.3f}",
                *ffmpeg_local_input(
                    source,
                    config.maximum_streams,
                    config.maximum_decoded_video_pixel_slots_per_frame,
                    config.maximum_decoded_audio_sample_slots_per_frame,
                ),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                (
                    # FFmpeg may decode more than one frame around a seek even
                    # with -frames:v 1. Select the exact frame handed to the
                    # encoder before observing it, so showinfo emits one PTS.
                    "select=eq(n\\,0),showinfo,"
                    f"scale={config.frame_width}:{config.frame_height}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={config.frame_width}:{config.frame_height}:"
                    "(ow-iw)/2:(oh-ih)/2:black"
                ),
                "-c:v",
                "png",
                "-f",
                "image2",
                "-update",
                "1",
                "-fs",
                str(config.maximum_frame_output_bytes),
                "-y",
                str(path.absolute()),
            ],
            timeout_seconds=config.tool_timeout_seconds,
            stdout_limit_bytes=config.stdout_limit_bytes,
            stderr_limit_bytes=config.stderr_limit_bytes,
            require_complete_stderr=True,
        )
        enforce_private_file(path)
        budget.record_file(path, maximum_file_bytes=config.maximum_frame_output_bytes)
        actual_timestamp_ms = _parse_extracted_frame_timestamp(
            result.stderr,
            duration_ms=probe.duration_ms,
            start_time_ms=probe.start_time_ms,
        )
        perceptual_hash = perceptual_hash_png(
            path,
            maximum_bytes=config.maximum_frame_output_bytes,
            maximum_pixels=config.frame_width * config.frame_height,
        )
        staged = StagedFrame(
            path=path,
            timestamp_ms=actual_timestamp_ms,
            perceptual_hash=perceptual_hash,
            reasons=candidate.reasons,
            candidate_timestamps_ms=candidate.source_timestamps_ms,
        )
        if merge_near_duplicate(
            retained,
            staged,
            maximum_distance=config.perceptual_hash_distance,
        ):
            path.unlink(missing_ok=True)
            duplicate_count += 1
    return sorted(
        retained, key=lambda frame: (frame.timestamp_ms, frame.path.name)
    ), duplicate_count
