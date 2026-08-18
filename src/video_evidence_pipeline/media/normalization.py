"""Private staging, verification, and immutable publication for normalized media."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from video_evidence_pipeline import __version__
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRef,
    ProducerIdentity,
    ProducerKind,
    artifact_file_matches,
)
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.job import JobSpec, PrimaryInputKind, primary_input_kind
from video_evidence_pipeline.domain.manifests import (
    StageManifest,
    StageStatus,
    stage_manifest_matches,
)
from video_evidence_pipeline.domain.media import (
    AnalysisProxy,
    FrameIndex,
    FrameRecord,
    MediaManifest,
    MediaProbe,
    MediaStreamType,
    NormalizationConfig,
    NormalizedSubtitle,
    SpeechAudio,
    SubtitleStatus,
    ToolIdentity,
)
from video_evidence_pipeline.domain.serialization import fingerprint, pretty_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.errors import integrity_error, io_error
from video_evidence_pipeline.media.budget import StagingBudget
from video_evidence_pipeline.media.commands import ffmpeg_local_input, ffprobe_local_input
from video_evidence_pipeline.media.frames import StagedFrame, extract_frames
from video_evidence_pipeline.media.probe import _milliseconds, probe_media
from video_evidence_pipeline.media.runner import FFmpegRunner
from video_evidence_pipeline.media.staging import prepare_private_output
from video_evidence_pipeline.media.subtitles import (
    StagedSubtitle,
    extract_subtitles,
    verify_cue_jsonl,
)
from video_evidence_pipeline.storage.atomic import (
    atomic_copy_exclusive,
    atomic_write_bytes,
    atomic_write_json,
    read_json_model,
)
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    enforce_private_file,
    ensure_private_directory,
)

NORMALIZATION_IMPLEMENTATION_VERSION = "media-normalization-v1"
_ATTEMPT_NAME = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class AudioProbeInfo:
    format_name: str
    codec_name: str
    duration_ms: int
    sample_rate_hz: int
    channels: int


@dataclass(frozen=True, slots=True)
class StagedMedia:
    attempt_directory: Path
    started_at: datetime
    primary_input_kind: PrimaryInputKind
    probe: MediaProbe
    analysis_path: Path | None
    analysis_probe: MediaProbe | None
    audio_path: Path | None
    audio_probe: AudioProbeInfo | None
    subtitles: tuple[StagedSubtitle, ...]
    frames: tuple[StagedFrame, ...]
    frame_candidate_count: int
    frame_duplicate_count: int
    staging_budget: StagingBudget
    ffmpeg: ToolIdentity
    ffprobe: ToolIdentity
    normalization_fingerprint: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    media_manifest: MediaManifest
    stage_manifest: StageManifest
    reused: bool


def normalization_fingerprint(
    raw_artifact: ArtifactRef,
    config: NormalizationConfig,
    ffmpeg: ToolIdentity,
    ffprobe: ToolIdentity,
    *,
    primary_input_kind: PrimaryInputKind = PrimaryInputKind.VIDEO,
) -> str:
    value = {
        "raw_sha256": raw_artifact.content_sha256,
        "configuration": config.model_dump(mode="json"),
        "ffmpeg": ffmpeg.model_dump(mode="json"),
        "ffprobe": ffprobe.model_dump(mode="json"),
        "implementation_version": NORMALIZATION_IMPLEMENTATION_VERSION,
    }
    # The historical video fingerprint is intentionally unchanged.
    if primary_input_kind is not PrimaryInputKind.VIDEO:
        value["primary_input_kind"] = primary_input_kind.value
    return fingerprint(value)


def transcript_primary_normalization_fingerprint(
    raw_artifact: ArtifactRef,
    config: NormalizationConfig,
) -> str:
    """Bind a supplied text primary to its immutable raw artifact, without media tools."""

    return fingerprint(
        {
            "raw_sha256": raw_artifact.content_sha256,
            "configuration": config.model_dump(mode="json"),
            "primary_input_kind": PrimaryInputKind.TRANSCRIPT.value,
            "implementation_version": NORMALIZATION_IMPLEMENTATION_VERSION,
        }
    )


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)() or path.is_mount()


def _staging_directory(workspace: JobWorkspace) -> Path:
    """Return the fixed staging root after refusing any link-shaped component."""
    normalized = workspace.root / "normalized"
    staging = normalized / "staging"
    for directory in (normalized, staging):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            try:
                ensure_private_directory(directory)
            except OSError:
                raise io_error(
                    "NORMALIZATION_STAGING_FAILED", "normalization staging is unavailable"
                ) from None
            continue
        except OSError:
            raise io_error(
                "NORMALIZATION_STAGING_FAILED", "normalization staging is unavailable"
            ) from None
        if _is_link_or_junction(directory) or not stat.S_ISDIR(metadata.st_mode):
            raise integrity_error("NORMALIZATION_STAGING_INVALID", "staging path is invalid")
        try:
            ensure_private_directory(directory)
        except OSError:
            raise io_error(
                "NORMALIZATION_STAGING_FAILED", "normalization staging is unavailable"
            ) from None
    return staging


def _validate_attempt_directory(attempt: Path, expected_parent: Path) -> None:
    try:
        if (
            attempt.parent != expected_parent
            or _ATTEMPT_NAME.fullmatch(attempt.name) is None
            or _is_link_or_junction(attempt)
            or not stat.S_ISDIR(attempt.lstat().st_mode)
            or attempt.resolve(strict=True).parent != expected_parent.resolve(strict=True)
        ):
            raise integrity_error("NORMALIZATION_STAGING_INVALID", "staging path is invalid")
        for current, directories, files in os.walk(attempt, topdown=True, followlinks=False):
            current_path = Path(current)
            try:
                current_path.relative_to(attempt)
            except ValueError:
                raise integrity_error(
                    "NORMALIZATION_STAGING_INVALID", "staging path is invalid"
                ) from None
            for name in [*directories, *files]:
                child = current_path / name
                if _is_link_or_junction(child):
                    raise integrity_error(
                        "NORMALIZATION_STAGING_INVALID", "staging path is invalid"
                    )
    except FileNotFoundError:
        raise
    except OSError:
        raise io_error("NORMALIZATION_STAGING_CLEANUP_FAILED", "staging cleanup failed") from None


def create_attempt_directory(workspace: JobWorkspace) -> Path:
    staging = _staging_directory(workspace)
    attempt = staging / uuid.uuid4().hex
    ensure_private_directory(attempt, exist_ok=False)
    return attempt


def cleanup_stale_attempts(workspace: JobWorkspace) -> None:
    """Remove only verified direct-child attempts while the normalization lock is held."""
    staging = _staging_directory(workspace)
    try:
        children = list(staging.iterdir())
    except OSError:
        raise io_error("NORMALIZATION_STAGING_CLEANUP_FAILED", "staging cleanup failed") from None
    for child in children:
        _validate_attempt_directory(child, staging)
        cleanup_attempt(workspace, child)


def cleanup_attempt(workspace: JobWorkspace, attempt: Path) -> None:
    expected_parent = _staging_directory(workspace)
    try:
        try:
            attempt.lstat()
        except FileNotFoundError:
            return
        _validate_attempt_directory(attempt, expected_parent)
        shutil.rmtree(attempt, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError:
        raise io_error("NORMALIZATION_STAGING_CLEANUP_FAILED", "staging cleanup failed") from None


def _run_analysis_proxy(
    runner: FFmpegRunner,
    source: Path,
    target: Path,
    config: NormalizationConfig,
) -> None:
    prepare_private_output(target)
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
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            (
                f"scale={config.analysis_width}:{config.analysis_height}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={config.analysis_width}:{config.analysis_height}:"
                "(ow-iw)/2:(oh-ih)/2:black"
            ),
            "-c:v",
            config.analysis_video_codec,
            "-preset",
            "medium",
            "-crf",
            str(config.analysis_crf),
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "1",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-fs",
            str(config.maximum_output_file_bytes),
            "-y",
            str(target.absolute()),
        ],
        timeout_seconds=config.tool_timeout_seconds,
        stdout_limit_bytes=config.stdout_limit_bytes,
        stderr_limit_bytes=config.stderr_limit_bytes,
    )


def _run_speech_audio(
    runner: FFmpegRunner,
    source: Path,
    target: Path,
    config: NormalizationConfig,
) -> None:
    prepare_private_output(target)
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
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(config.speech_sample_rate_hz),
            "-c:a",
            "pcm_s16le",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-f",
            "wav",
            "-fs",
            str(config.maximum_output_file_bytes),
            "-y",
            str(target.absolute()),
        ],
        timeout_seconds=config.tool_timeout_seconds,
        stdout_limit_bytes=config.stdout_limit_bytes,
        stderr_limit_bytes=config.stderr_limit_bytes,
    )


def _speech_output_upper_bound(duration_ms: int, config: NormalizationConfig) -> int:
    return min(
        config.maximum_output_file_bytes,
        (duration_ms * config.speech_sample_rate_hz * 2 + 999) // 1000 + 44,
    )


def _audio_probe(
    runner: FFmpegRunner,
    path: Path,
    config: NormalizationConfig,
) -> AudioProbeInfo:
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
                path,
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
    try:
        root = json.loads(result.stdout.decode("utf-8"))
        streams = root["streams"]
        format_data = root["format"]
        if not isinstance(streams, list) or len(streams) != 1:
            raise ValueError
        stream = streams[0]
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            raise ValueError
        duration_ms = _milliseconds(
            format_data["duration"],
            required=True,
            code="NORMALIZED_AUDIO_INVALID",
        )
        if duration_ms is None:
            raise ValueError
        info = AudioProbeInfo(
            format_name=str(format_data["format_name"]),
            codec_name=str(stream["codec_name"]),
            duration_ms=duration_ms,
            sample_rate_hz=int(stream["sample_rate"]),
            channels=int(stream["channels"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError, UnicodeError):
        raise integrity_error("NORMALIZED_AUDIO_INVALID", "speech audio probe is invalid") from None
    if (
        info.format_name != "wav"
        or info.codec_name != "pcm_s16le"
        or info.duration_ms <= 0
        or info.duration_ms > config.maximum_duration_ms + config.duration_tolerance_ms
        or info.sample_rate_hz != config.speech_sample_rate_hz
        or info.channels != 1
    ):
        raise integrity_error("NORMALIZED_AUDIO_INVALID", "speech audio contract is invalid")
    return info


def _verify_staged_output(
    path: Path,
    config: NormalizationConfig,
    *,
    maximum_file_bytes: int,
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum_file_bytes
        ):
            raise integrity_error("NORMALIZED_OUTPUT_INVALID", "normalized output is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "NORMALIZED_OUTPUT_INVALID", "normalized output is unavailable"
        ) from None


def process_media(
    runner: FFmpegRunner,
    source: Path,
    raw_artifact: ArtifactRef,
    config: NormalizationConfig,
    attempt: Path,
    ffmpeg: ToolIdentity,
    ffprobe: ToolIdentity,
    *,
    primary_input_kind: PrimaryInputKind = PrimaryInputKind.VIDEO,
) -> StagedMedia:
    started_at = datetime.now(UTC)
    budget = StagingBudget(config.maximum_output_bytes)
    probe = probe_media(runner, source, config)
    if primary_input_kind is PrimaryInputKind.AUDIO:
        if any(stream.stream_type is MediaStreamType.VIDEO for stream in probe.streams):
            raise integrity_error(
                "AUDIO_PRIMARY_HAS_VIDEO", "audio primary input must not contain a video stream"
            )
        if not any(stream.stream_type is MediaStreamType.AUDIO for stream in probe.streams):
            raise integrity_error(
                "AUDIO_STREAM_UNAVAILABLE", "audio primary input has no audio stream"
            )
        audio_path = attempt / "speech.wav"
        budget.require_capacity(_speech_output_upper_bound(probe.duration_ms, config))
        _run_speech_audio(runner, source, audio_path, config)
        _verify_staged_output(
            audio_path,
            config,
            maximum_file_bytes=config.maximum_output_file_bytes,
        )
        budget.record_file(audio_path, maximum_file_bytes=config.maximum_output_file_bytes)
        audio_probe = _audio_probe(runner, audio_path, config)
        if abs(audio_probe.duration_ms - probe.duration_ms) > config.duration_tolerance_ms:
            raise integrity_error(
                "NORMALIZED_AUDIO_INVALID", "speech audio duration does not match source"
            )
        return StagedMedia(
            attempt_directory=attempt,
            started_at=started_at,
            primary_input_kind=primary_input_kind,
            probe=probe,
            analysis_path=None,
            analysis_probe=None,
            audio_path=audio_path,
            audio_probe=audio_probe,
            subtitles=(),
            frames=(),
            frame_candidate_count=0,
            frame_duplicate_count=0,
            staging_budget=budget,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            normalization_fingerprint=normalization_fingerprint(
                raw_artifact,
                config,
                ffmpeg,
                ffprobe,
                primary_input_kind=primary_input_kind,
            ),
            warnings=(),
        )
    analysis_path = attempt / "analysis.mp4"
    budget.require_capacity(config.maximum_output_file_bytes)
    _run_analysis_proxy(runner, source, analysis_path, config)
    _verify_staged_output(
        analysis_path,
        config,
        maximum_file_bytes=config.maximum_output_file_bytes,
    )
    budget.record_file(analysis_path, maximum_file_bytes=config.maximum_output_file_bytes)
    analysis_probe = probe_media(runner, analysis_path, config)
    analysis_video = [
        stream for stream in analysis_probe.streams if stream.stream_type is MediaStreamType.VIDEO
    ]
    if (
        len(analysis_video) != 1
        or len(analysis_probe.streams) != 1
        or analysis_video[0].codec_name != "h264"
        or analysis_video[0].width != config.analysis_width
        or analysis_video[0].height != config.analysis_height
        or abs(analysis_probe.duration_ms - probe.duration_ms) > config.duration_tolerance_ms
    ):
        raise integrity_error("ANALYSIS_PROXY_INVALID", "analysis proxy contract is invalid")
    has_audio = any(stream.stream_type is MediaStreamType.AUDIO for stream in probe.streams)
    audio_path = attempt / "speech.wav" if has_audio else None
    audio_probe = None
    warnings: list[str] = []
    if audio_path is not None:
        budget.require_capacity(_speech_output_upper_bound(probe.duration_ms, config))
        _run_speech_audio(runner, source, audio_path, config)
        _verify_staged_output(
            audio_path,
            config,
            maximum_file_bytes=config.maximum_output_file_bytes,
        )
        budget.record_file(audio_path, maximum_file_bytes=config.maximum_output_file_bytes)
        audio_probe = _audio_probe(runner, audio_path, config)
        if abs(audio_probe.duration_ms - probe.duration_ms) > config.duration_tolerance_ms:
            raise integrity_error(
                "NORMALIZED_AUDIO_INVALID", "speech audio duration does not match source"
            )
    else:
        warnings.append("AUDIO_STREAM_UNAVAILABLE")
    subtitles = tuple(extract_subtitles(runner, source, probe, config, attempt, budget))
    warnings.extend(
        subtitle.warning_code for subtitle in subtitles if subtitle.warning_code is not None
    )
    frames, duplicate_count = extract_frames(runner, source, probe, config, attempt, budget)
    if not frames:
        raise integrity_error("FRAME_EXTRACTION_EMPTY", "no frame could be extracted")
    for frame in frames:
        _verify_staged_output(
            frame.path,
            config,
            maximum_file_bytes=config.maximum_frame_output_bytes,
        )
    return StagedMedia(
        attempt_directory=attempt,
        started_at=started_at,
        primary_input_kind=primary_input_kind,
        probe=probe,
        analysis_path=analysis_path,
        analysis_probe=analysis_probe,
        audio_path=audio_path,
        audio_probe=audio_probe,
        subtitles=subtitles,
        frames=tuple(frames),
        frame_candidate_count=len(frames) + duplicate_count,
        frame_duplicate_count=duplicate_count,
        staging_budget=budget,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        normalization_fingerprint=normalization_fingerprint(
            raw_artifact,
            config,
            ffmpeg,
            ffprobe,
            primary_input_kind=primary_input_kind,
        ),
        warnings=tuple(sorted(set(warnings))),
    )


def _artifact_for(
    workspace: JobWorkspace,
    staged_path: Path,
    *,
    artifact_prefix: str,
    artifact_type: str,
    relative_directory: str,
    filename: str,
) -> ArtifactRef:
    try:
        size = staged_path.stat(follow_symlinks=False).st_size
        content_sha256 = sha256_file(staged_path)
    except OSError:
        raise integrity_error(
            "NORMALIZED_OUTPUT_INVALID", "normalized output is unavailable"
        ) from None
    relative_path = f"normalized/{relative_directory}/{content_sha256}/{filename}"
    artifact = ArtifactRef(
        artifact_id=f"{artifact_prefix}-{content_sha256[:16]}",
        artifact_type=artifact_type,
        content_sha256=content_sha256,
        size_bytes=size,
        relative_storage_path=relative_path,
    )
    ensure_private_directory(workspace.safe_path(f"normalized/{relative_directory}"))
    target = workspace.safe_path(relative_path)
    ensure_private_directory(target.parent)
    atomic_copy_exclusive(
        staged_path,
        target,
        verifier=lambda candidate: _verify_artifact_path(candidate, artifact),
    )
    _verify_artifact_path(target, artifact)
    return artifact


def _verify_artifact_path(path: Path, artifact: ArtifactRef, *, deep: bool = False) -> None:
    try:
        if not artifact_file_matches(path, artifact, deep=deep, hasher=sha256_file):
            raise integrity_error("NORMALIZED_BUNDLE_TAMPERED", "normalized artifact is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "NORMALIZED_BUNDLE_TAMPERED", "normalized artifact is unavailable"
        ) from None


def _stage_manifest(manifest: MediaManifest, output_artifacts: list[ArtifactRef]) -> StageManifest:
    return StageManifest(
        job_id=manifest.job_id,
        stage="normalization",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.NORMALIZED,
        input_artifacts=[manifest.raw_artifact],
        output_artifacts=output_artifacts,
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="ffmpeg-normalization",
            version=__version__,
        ),
        configuration_sha256=manifest.normalization_fingerprint,
        started_at=manifest.started_at,
        finished_at=manifest.created_at,
        warnings=list(manifest.warnings),
    )


def publish_staged_media(
    workspace: JobWorkspace,
    job: JobSpec,
    raw_artifact: ArtifactRef,
    config: NormalizationConfig,
    staged: StagedMedia,
) -> NormalizationResult:
    analysis = None
    audio = None
    output_artifacts: list[ArtifactRef] = []
    if staged.primary_input_kind is PrimaryInputKind.VIDEO:
        if staged.analysis_path is None or staged.analysis_probe is None:
            raise integrity_error(
                "NORMALIZED_BUNDLE_INVALID", "video analysis staging is incomplete"
            )
        analysis_artifact = _artifact_for(
            workspace,
            staged.analysis_path,
            artifact_prefix="analysis",
            artifact_type="analysis_proxy",
            relative_directory="analysis",
            filename="analysis.mp4",
        )
        analysis_stream = next(
            stream
            for stream in staged.analysis_probe.streams
            if stream.stream_type is MediaStreamType.VIDEO
        )
        analysis = AnalysisProxy(
            artifact=analysis_artifact,
            format_name=staged.analysis_probe.format_name,
            codec_name="h264",
            width=analysis_stream.width,
            height=analysis_stream.height,
            duration_ms=staged.analysis_probe.duration_ms,
        )
        output_artifacts.append(analysis_artifact)
    if staged.audio_path is not None and staged.audio_probe is not None:
        audio_artifact = _artifact_for(
            workspace,
            staged.audio_path,
            artifact_prefix="speech",
            artifact_type="speech_audio",
            relative_directory="audio",
            filename="speech.wav",
        )
        output_artifacts.append(audio_artifact)
        audio = SpeechAudio(
            artifact=audio_artifact,
            format_name="wav",
            codec_name="pcm_s16le",
            sample_rate_hz=staged.audio_probe.sample_rate_hz,
            channels=1,
            duration_ms=staged.audio_probe.duration_ms,
        )
    subtitles: list[NormalizedSubtitle] = []
    for staged_subtitle in staged.subtitles:
        stream = staged_subtitle.stream
        webvtt_artifact = cues_artifact = None
        if staged_subtitle.status is SubtitleStatus.NORMALIZED:
            if staged_subtitle.webvtt_path is None or staged_subtitle.cues_path is None:
                raise integrity_error(
                    "NORMALIZED_BUNDLE_INVALID", "normalized subtitle staging is incomplete"
                )
            webvtt_artifact = _artifact_for(
                workspace,
                staged_subtitle.webvtt_path,
                artifact_prefix=f"subtitle-{stream.index}",
                artifact_type="normalized_subtitle_webvtt",
                relative_directory="subtitles",
                filename=f"subtitle-{stream.index:04d}.vtt",
            )
            cues_artifact = _artifact_for(
                workspace,
                staged_subtitle.cues_path,
                artifact_prefix=f"subtitle-cues-{stream.index}",
                artifact_type="normalized_subtitle_cues",
                relative_directory="subtitles",
                filename=f"subtitle-{stream.index:04d}.jsonl",
            )
            output_artifacts.extend([webvtt_artifact, cues_artifact])
        subtitles.append(
            NormalizedSubtitle(
                stream_index=stream.index,
                codec_name=stream.codec_name,
                language=stream.language,
                kind=stream.subtitle_kind,
                provenance=stream.subtitle_provenance,
                is_default=stream.is_default,
                is_forced=stream.is_forced,
                status=staged_subtitle.status,
                webvtt_artifact=webvtt_artifact,
                cues_artifact=cues_artifact,
                cue_count=len(staged_subtitle.cues),
                warning_code=staged_subtitle.warning_code,
                raw_text_preserved=False,
            )
        )
    frame_index = None
    frame_index_artifact = None
    if staged.primary_input_kind is PrimaryInputKind.VIDEO:
        frame_records: list[FrameRecord] = []
        for index, staged_frame in enumerate(staged.frames, start=1):
            frame_id = f"F{index:06d}"
            frame_artifact = _artifact_for(
                workspace,
                staged_frame.path,
                artifact_prefix=f"frame-{frame_id.lower()}",
                artifact_type="extracted_frame",
                relative_directory="frames",
                filename=f"{frame_id}.png",
            )
            output_artifacts.append(frame_artifact)
            frame_records.append(
                FrameRecord(
                    frame_id=frame_id,
                    timestamp_ms=staged_frame.timestamp_ms,
                    artifact=frame_artifact,
                    perceptual_hash=staged_frame.perceptual_hash,
                    reasons=list(staged_frame.reasons),
                    candidate_timestamps_ms=list(staged_frame.candidate_timestamps_ms),
                )
            )
        frame_index = FrameIndex(
            duration_ms=staged.probe.duration_ms,
            records=frame_records,
            candidate_count=staged.frame_candidate_count,
            duplicate_count=staged.frame_duplicate_count,
        )
        frame_index_staging = staged.attempt_directory / "frame-index.json"
        frame_index_payload = pretty_json_bytes(frame_index)
        staged.staging_budget.require_capacity(len(frame_index_payload))
        atomic_write_bytes(frame_index_staging, frame_index_payload, private=True)
        _verify_staged_output(
            frame_index_staging,
            config,
            maximum_file_bytes=config.maximum_output_file_bytes,
        )
        staged.staging_budget.record_file(
            frame_index_staging,
            maximum_file_bytes=config.maximum_output_file_bytes,
        )
        frame_index_artifact = _artifact_for(
            workspace,
            frame_index_staging,
            artifact_prefix="frame-index",
            artifact_type="frame_index",
            relative_directory="index",
            filename="frame-index.json",
        )
        output_artifacts.append(frame_index_artifact)
    created_at = datetime.now(UTC)
    try:
        media_manifest = MediaManifest(
            job_id=job.job_id,
            normalization_fingerprint=staged.normalization_fingerprint,
            implementation_version=NORMALIZATION_IMPLEMENTATION_VERSION,
            configuration=config,
            primary_input_kind=staged.primary_input_kind,
            ffmpeg=staged.ffmpeg,
            ffprobe=staged.ffprobe,
            raw_artifact=raw_artifact,
            probe=staged.probe,
            analysis_proxy=analysis,
            speech_audio=audio,
            subtitles=subtitles,
            frame_index_artifact=frame_index_artifact,
            frame_index=frame_index,
            warnings=list(staged.warnings),
            started_at=staged.started_at,
            created_at=created_at,
        )
    except ValidationError:
        raise integrity_error(
            "NORMALIZED_BUNDLE_INVALID", "normalized bundle is inconsistent"
        ) from None
    stage_manifest = _stage_manifest(media_manifest, output_artifacts)
    if workspace.media_manifest_json.exists() or workspace.normalization_manifest_json.exists():
        existing = load_normalization_result(workspace, job, raw_artifact, expected_config=config)
        return existing
    atomic_write_json(workspace.media_manifest_json, media_manifest, private=True)
    atomic_write_json(workspace.normalization_manifest_json, stage_manifest, private=True)
    return NormalizationResult(
        media_manifest=media_manifest, stage_manifest=stage_manifest, reused=False
    )


def _manifest_artifacts(manifest: MediaManifest) -> list[ArtifactRef]:
    return [
        *([manifest.analysis_proxy.artifact] if manifest.analysis_proxy is not None else []),
        *([manifest.speech_audio.artifact] if manifest.speech_audio is not None else []),
        *(
            artifact
            for subtitle in manifest.subtitles
            for artifact in (subtitle.webvtt_artifact, subtitle.cues_artifact)
            if artifact is not None
        ),
        *(
            record.artifact
            for record in (manifest.frame_index.records if manifest.frame_index is not None else [])
        ),
        *([manifest.frame_index_artifact] if manifest.frame_index_artifact is not None else []),
    ]


def verify_normalized_bundle(
    workspace: JobWorkspace,
    job: JobSpec,
    raw_artifact: ArtifactRef,
    *,
    expected_config: NormalizationConfig | None = None,
    deep: bool = False,
) -> MediaManifest:
    manifest = read_json_model(
        workspace.media_manifest_json, MediaManifest, code="MEDIA_MANIFEST_INVALID"
    )
    if (
        manifest.job_id != job.job_id
        or manifest.raw_artifact != raw_artifact
        or manifest.primary_input_kind is not primary_input_kind(job)
        or manifest.implementation_version != NORMALIZATION_IMPLEMENTATION_VERSION
        or (expected_config is not None and manifest.configuration != expected_config)
    ):
        raise integrity_error("MEDIA_MANIFEST_MISMATCH", "media manifest does not match the job")
    if manifest.primary_input_kind is PrimaryInputKind.TRANSCRIPT:
        expected_fingerprint = transcript_primary_normalization_fingerprint(
            raw_artifact, manifest.configuration
        )
    else:
        if manifest.ffmpeg is None or manifest.ffprobe is None:
            raise integrity_error("MEDIA_MANIFEST_MISMATCH", "media tool identity is missing")
        expected_fingerprint = normalization_fingerprint(
            raw_artifact,
            manifest.configuration,
            manifest.ffmpeg,
            manifest.ffprobe,
            primary_input_kind=manifest.primary_input_kind,
        )
    if deep and manifest.normalization_fingerprint != expected_fingerprint:
        raise integrity_error("MEDIA_MANIFEST_MISMATCH", "media manifest does not match the job")
    artifacts = _manifest_artifacts(manifest)
    if len({artifact.relative_storage_path for artifact in artifacts}) != len(artifacts):
        raise integrity_error("MEDIA_MANIFEST_MISMATCH", "media artifact paths are not unique")
    for artifact in artifacts:
        if not artifact.relative_storage_path.startswith("normalized/"):
            raise integrity_error("MEDIA_MANIFEST_MISMATCH", "media artifact path is invalid")
        _verify_artifact_path(
            workspace.safe_path(artifact.relative_storage_path),
            artifact,
            deep=deep,
        )
    if manifest.frame_index_artifact is not None and manifest.frame_index is not None:
        stored_index = read_json_model(
            workspace.safe_path(manifest.frame_index_artifact.relative_storage_path),
            FrameIndex,
            code="FRAME_INDEX_INVALID",
        )
        if stored_index != manifest.frame_index:
            raise integrity_error(
                "FRAME_INDEX_INVALID", "frame index does not match media manifest"
            )
    for subtitle in manifest.subtitles:
        if subtitle.status is not SubtitleStatus.NORMALIZED:
            continue
        if subtitle.cues_artifact is None:
            raise integrity_error("SUBTITLE_CUES_INVALID", "subtitle cue artifact is missing")
        cue_path = workspace.safe_path(subtitle.cues_artifact.relative_storage_path)
        cues = verify_cue_jsonl(
            cue_path,
            maximum_bytes=manifest.configuration.maximum_subtitle_bytes,
        )
        if len(cues) != subtitle.cue_count or any(
            manifest.probe is None
            or cue.end_ms
            > manifest.probe.duration_ms + manifest.configuration.duration_tolerance_ms
            or not cue.text
            for cue in cues
        ):
            raise integrity_error("SUBTITLE_CUES_INVALID", "subtitle cue JSONL is inconsistent")
    return manifest


def publish_transcript_primary(
    workspace: JobWorkspace,
    job: JobSpec,
    raw_artifact: ArtifactRef,
    config: NormalizationConfig,
) -> NormalizationResult:
    """Publish the no-media normalization projection for a supplied transcript.

    The source acquisition artifact remains the sole immutable source.  This
    explicit manifest lets the rest of the state machine keep the existing
    normalization gate without invoking FFmpeg or inventing media metadata.
    """

    fingerprint_value = transcript_primary_normalization_fingerprint(raw_artifact, config)
    if workspace.media_manifest_json.exists() or workspace.normalization_manifest_json.exists():
        existing = load_normalization_result(workspace, job, raw_artifact, expected_config=config)
        return existing
    started_at = datetime.now(UTC)
    manifest = MediaManifest(
        job_id=job.job_id,
        normalization_fingerprint=fingerprint_value,
        implementation_version=NORMALIZATION_IMPLEMENTATION_VERSION,
        configuration=config,
        primary_input_kind=PrimaryInputKind.TRANSCRIPT,
        raw_artifact=raw_artifact,
        warnings=["TRANSCRIPT_PRIMARY"],
        started_at=started_at,
        created_at=datetime.now(UTC),
    )
    stage = _stage_manifest(manifest, [])
    atomic_write_json(workspace.media_manifest_json, manifest, private=True)
    atomic_write_json(workspace.normalization_manifest_json, stage, private=True)
    return NormalizationResult(media_manifest=manifest, stage_manifest=stage, reused=False)


def load_normalization_result(
    workspace: JobWorkspace,
    job: JobSpec,
    raw_artifact: ArtifactRef,
    *,
    expected_config: NormalizationConfig | None = None,
    deep: bool = False,
) -> NormalizationResult:
    manifest = verify_normalized_bundle(
        workspace, job, raw_artifact, expected_config=expected_config, deep=deep
    )
    stage_manifest = read_json_model(
        workspace.normalization_manifest_json,
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    artifacts = _manifest_artifacts(manifest)
    expected = _stage_manifest(manifest, artifacts)
    if not stage_manifest_matches(stage_manifest, expected, deep=deep):
        raise integrity_error("STAGE_MANIFEST_MISMATCH", "normalization stage manifest is invalid")
    return NormalizationResult(media_manifest=manifest, stage_manifest=stage_manifest, reused=True)


def recover_normalization_result(
    workspace: JobWorkspace,
    job: JobSpec,
    raw_artifact: ArtifactRef,
    *,
    expected_config: NormalizationConfig | None = None,
) -> NormalizationResult | None:
    media_exists = workspace.media_manifest_json.exists()
    stage_exists = workspace.normalization_manifest_json.exists()
    if not media_exists and not stage_exists:
        return None
    if not media_exists:
        raise integrity_error("MEDIA_MANIFEST_INVALID", "normalization stage lacks media manifest")
    manifest = verify_normalized_bundle(
        workspace, job, raw_artifact, expected_config=expected_config
    )
    if not stage_exists:
        atomic_write_json(
            workspace.normalization_manifest_json,
            _stage_manifest(manifest, _manifest_artifacts(manifest)),
            private=True,
        )
    return load_normalization_result(workspace, job, raw_artifact, expected_config=expected_config)
