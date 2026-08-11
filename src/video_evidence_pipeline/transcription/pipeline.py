"""Captions-first transcript assembly, publication, and verification."""

from __future__ import annotations

import re
import shutil
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline import __version__
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRef,
    ProducerIdentity,
    ProducerKind,
    artifact_file_matches,
)
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.job import JobSpec
from video_evidence_pipeline.domain.manifests import StageManifest, StageStatus
from video_evidence_pipeline.domain.media import (
    MediaManifest,
    NormalizedSubtitle,
    SubtitleCue,
    SubtitleProvenance,
    SubtitleStatus,
)
from video_evidence_pipeline.domain.serialization import (
    canonical_json_bytes,
    fingerprint,
)
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transcription import (
    AsrSegment,
    GapPolicy,
    OverlapPolicy,
    SegmentLineage,
    TranscriptConfidence,
    TranscriptConfidenceKind,
    TranscriptInterval,
    TranscriptionBackendIdentity,
    TranscriptionConfig,
    TranscriptManifest,
    TranscriptSegment,
    TranscriptSource,
)
from video_evidence_pipeline.errors import integrity_error, state_error
from video_evidence_pipeline.media.subtitles import verify_cue_jsonl
from video_evidence_pipeline.storage.atomic import (
    atomic_copy_exclusive,
    atomic_write_bytes,
    atomic_write_json,
)
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    enforce_private_file,
    ensure_private_directory,
)
from video_evidence_pipeline.transcription.base import AsrBackend

TRANSCRIPTION_IMPLEMENTATION_VERSION = "transcription-v1"
_ATTEMPT_NAME = re.compile(r"^[0-9a-f]{32}$")
_MAXIMUM_MANIFEST_BYTES = 16 * 1024 * 1024


def _read_bounded_json_model[ModelT: BaseModel](
    path: Path, model: type[ModelT], *, code: str
) -> ModelT:
    try:
        before = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAXIMUM_MANIFEST_BYTES
        ):
            raise integrity_error(code, "required machine artifact is malformed")
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError:
        raise integrity_error(code, "required machine artifact is unavailable") from None
    if len(payload) != before.st_size or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise integrity_error(code, "required machine artifact changed while reading")
    try:
        return model.model_validate_json(payload, strict=True)
    except (UnicodeError, ValidationError):
        raise integrity_error(code, "required machine artifact is malformed") from None


@dataclass(frozen=True, slots=True)
class PreparedTranscript:
    segments: tuple[TranscriptSegment, ...]
    configuration: TranscriptionConfig
    backends: tuple[TranscriptionBackendIdentity, ...]
    selected_source_artifacts: tuple[ArtifactRef, ...]
    selected_subtitle_stream_index: int | None
    selected_subtitle_provenance: SubtitleProvenance | None
    caption_coverage: float | None
    media_bundle_fingerprint: str
    transcription_fingerprint: str
    duration_ms: int
    gaps: tuple[TranscriptInterval, ...]
    overlaps: tuple[TranscriptInterval, ...]
    warnings: tuple[str, ...]
    started_at: datetime


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    transcript_manifest: TranscriptManifest
    stage_manifest: StageManifest
    segments: tuple[TranscriptSegment, ...]
    reused: bool


def normalize_transcript_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def media_bundle_fingerprint(manifest: MediaManifest) -> str:
    artifacts = [
        manifest.analysis_proxy.artifact,
        *([manifest.speech_audio.artifact] if manifest.speech_audio is not None else []),
        *(
            artifact
            for subtitle in manifest.subtitles
            for artifact in (subtitle.webvtt_artifact, subtitle.cues_artifact)
            if artifact is not None
        ),
        *(record.artifact for record in manifest.frame_index.records),
        manifest.frame_index_artifact,
    ]
    return fingerprint(
        {
            "normalization_fingerprint": manifest.normalization_fingerprint,
            "duration_ms": manifest.probe.duration_ms,
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        }
    )


def transcription_fingerprint(
    *,
    media_fingerprint: str,
    selected_artifacts: tuple[ArtifactRef, ...],
    configuration: TranscriptionConfig,
    backends: tuple[TranscriptionBackendIdentity, ...],
) -> str:
    return fingerprint(
        {
            "media_bundle_fingerprint": media_fingerprint,
            "selected_artifact_hashes": [item.content_sha256 for item in selected_artifacts],
            "configuration": configuration.model_dump(mode="json"),
            "backends": [item.model_dump(mode="json") for item in backends],
            "implementation_version": TRANSCRIPTION_IMPLEMENTATION_VERSION,
        }
    )


def _subtitle_order(
    subtitle: NormalizedSubtitle,
    preferred_languages: list[str],
) -> tuple[int, int, int, int, int]:
    language = (subtitle.language or "").casefold()
    ranks = {value.casefold(): index for index, value in enumerate(preferred_languages)}
    provenance_rank = {
        SubtitleProvenance.OFFICIAL: 0,
        SubtitleProvenance.EMBEDDED: 1,
        SubtitleProvenance.SIDECAR: 2,
    }
    return (
        ranks.get(language, len(ranks) + 1),
        provenance_rank[subtitle.provenance],
        0 if subtitle.is_default else 1,
        0 if not subtitle.is_forced else 1,
        subtitle.stream_index,
    )


def _caption_coverage(cues: tuple[SubtitleCue, ...], duration_ms: int) -> float:
    intervals = sorted((cue.start_ms, cue.end_ms) for cue in cues)
    covered = 0
    cursor = 0
    for start, end in intervals:
        start = max(0, min(start, duration_ms))
        end = max(0, min(end, duration_ms))
        if end <= start:
            continue
        if start > cursor:
            covered += end - start
            cursor = end
        elif end > cursor:
            covered += end - cursor
            cursor = end
    return covered / duration_ms


def _intervals(
    segments: tuple[TranscriptSegment, ...], duration_ms: int
) -> tuple[tuple[TranscriptInterval, ...], tuple[TranscriptInterval, ...]]:
    gaps: list[TranscriptInterval] = []
    overlaps: list[TranscriptInterval] = []
    starts: dict[int, list[str]] = {}
    ends: dict[int, list[str]] = {}
    for segment in segments:
        starts.setdefault(segment.start_ms, []).append(segment.segment_id)
        ends.setdefault(segment.end_ms, []).append(segment.segment_id)
    boundaries = sorted({0, duration_ms, *starts, *ends})
    active: set[str] = set()
    for index, start_ms in enumerate(boundaries[:-1]):
        active.difference_update(ends.get(start_ms, ()))
        active.update(starts.get(start_ms, ()))
        end_ms = boundaries[index + 1]
        if end_ms <= start_ms:
            continue
        if not active:
            if gaps and gaps[-1].end_ms == start_ms:
                previous = gaps.pop()
                gaps.append(TranscriptInterval(start_ms=previous.start_ms, end_ms=end_ms))
            else:
                gaps.append(TranscriptInterval(start_ms=start_ms, end_ms=end_ms))
        elif len(active) >= 2:
            if len(active) > 64:
                raise state_error(
                    "TRANSCRIPT_OVERLAP_TOO_COMPLEX",
                    "more than 64 transcript segments overlap at one timestamp",
                )
            active_ids = sorted(active)
            if (
                overlaps
                and overlaps[-1].end_ms == start_ms
                and overlaps[-1].segment_ids == active_ids
            ):
                previous = overlaps.pop()
                overlaps.append(
                    TranscriptInterval(
                        start_ms=previous.start_ms,
                        end_ms=end_ms,
                        segment_ids=active_ids,
                    )
                )
            else:
                overlaps.append(
                    TranscriptInterval(
                        start_ms=start_ms,
                        end_ms=end_ms,
                        segment_ids=active_ids,
                    )
                )
    return tuple(gaps), tuple(overlaps)


def _validate_segments(
    raw_segments: list[
        tuple[
            int,
            int,
            str,
            str | None,
            TranscriptSource,
            TranscriptConfidence,
            list[str],
            SegmentLineage,
        ]
    ],
    config: TranscriptionConfig,
    duration_ms: int,
) -> tuple[
    tuple[TranscriptSegment, ...], tuple[TranscriptInterval, ...], tuple[TranscriptInterval, ...]
]:
    raw_segments.sort(key=lambda item: (item[0], item[1], item[7].source_record_id))
    if not raw_segments:
        raise state_error("TRANSCRIPT_EMPTY", "transcription produced no usable segments")
    if len(raw_segments) > config.maximum_segments:
        raise state_error("TRANSCRIPT_TOO_LARGE", "transcription produced too many segments")
    segments: list[TranscriptSegment] = []
    total_chars = 0
    try:
        for index, (start, end, raw_text, speaker, source, confidence, flags, lineage) in enumerate(
            raw_segments, start=1
        ):
            normalized = normalize_transcript_text(raw_text)
            if end > duration_ms or end <= start or not normalized:
                raise state_error(
                    "TRANSCRIPT_TIMESTAMP_INVALID",
                    "transcript segment timestamps or text are invalid",
                )
            total_chars += len(raw_text) + len(normalized)
            if total_chars > config.maximum_total_text_chars:
                raise state_error("TRANSCRIPT_TOO_LARGE", "transcript text exceeds its limit")
            segments.append(
                TranscriptSegment(
                    segment_id=f"T{index:06d}",
                    start_ms=start,
                    end_ms=end,
                    speaker=speaker,
                    raw_text=raw_text,
                    normalized_text=normalized,
                    source=source,
                    confidence=confidence,
                    flags=sorted(set(flags)),
                    lineage=lineage,
                )
            )
    except ValidationError:
        raise integrity_error("TRANSCRIPT_INVALID", "transcript segment data is invalid") from None
    result = tuple(segments)
    gaps, overlaps = _intervals(result, duration_ms)
    if gaps and config.gap_policy is GapPolicy.REJECT:
        raise state_error("TRANSCRIPT_GAP_REJECTED", "transcript contains a disallowed gap")
    if overlaps and config.overlap_policy is OverlapPolicy.REJECT:
        raise state_error("TRANSCRIPT_OVERLAP_REJECTED", "transcript contains a disallowed overlap")
    return result, gaps, overlaps


def prepare_transcript(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    config: TranscriptionConfig,
    backend: AsrBackend | None,
    model_directory: Path | None = None,
) -> PreparedTranscript:
    started_at = datetime.now(UTC)
    preferred = list(config.preferred_languages)
    if job.assignment.language.casefold() not in {item.casefold() for item in preferred}:
        preferred.append(job.assignment.language)
    candidates = sorted(
        (
            item
            for item in media_manifest.subtitles
            if item.status is SubtitleStatus.NORMALIZED
            and item.cues_artifact is not None
            and item.cue_count > 0
        ),
        key=lambda item: _subtitle_order(item, preferred),
    )
    selected: NormalizedSubtitle | None = None
    selected_cues: tuple[SubtitleCue, ...] = ()
    coverage: float | None = None
    if candidates:
        selected = candidates[0]
        selected_cues = verify_cue_jsonl(
            workspace.safe_path(selected.cues_artifact.relative_storage_path),
            maximum_bytes=media_manifest.configuration.maximum_subtitle_bytes,
        )
        coverage = _caption_coverage(selected_cues, media_manifest.probe.duration_ms)
    raw: list[
        tuple[
            int,
            int,
            str,
            str | None,
            TranscriptSource,
            TranscriptConfidence,
            list[str],
            SegmentLineage,
        ]
    ] = []
    warnings: list[str] = []
    selected_artifacts_list: list[ArtifactRef] = []
    backend_identities: list[TranscriptionBackendIdentity] = []
    selected_stream_index: int | None = None
    selected_provenance: SubtitleProvenance | None = None
    asr_intervals: tuple[TranscriptInterval, ...] = ()
    if selected is not None and coverage is not None:
        if selected.cues_artifact is None:
            raise integrity_error("TRANSCRIPT_SOURCE_INVALID", "selected subtitle is incomplete")
        selected_artifacts_list.append(selected.cues_artifact)
        selected_stream_index = selected.stream_index
        selected_provenance = selected.provenance
        caption_backend = TranscriptionBackendIdentity(
            source=TranscriptSource.NORMALIZED_SUBTITLE,
            name="normalized-subtitle",
            version=media_manifest.implementation_version,
            model=f"{selected.provenance.value}:stream-{selected.stream_index}",
            model_fingerprint=selected.cues_artifact.content_sha256,
            local_files_only=True,
        )
        backend_identities.append(caption_backend)
        caption_backend_fingerprint = fingerprint(caption_backend)
        for cue in selected_cues:
            raw.append(
                (
                    cue.start_ms,
                    cue.end_ms,
                    cue.text,
                    None,
                    TranscriptSource.NORMALIZED_SUBTITLE,
                    TranscriptConfidence(kind=TranscriptConfidenceKind.NOT_AVAILABLE),
                    [],
                    SegmentLineage(
                        source_artifact=selected.cues_artifact,
                        source_stream_index=selected.stream_index,
                        subtitle_provenance=selected.provenance,
                        source_record_id=cue.cue_id,
                        backend_identity_fingerprint=caption_backend_fingerprint,
                    ),
                )
            )
        if coverage < config.minimum_caption_coverage:
            warnings.append("CAPTION_COVERAGE_INSUFFICIENT")
            cursor = 0
            uncovered: list[TranscriptInterval] = []
            for cue in sorted(selected_cues, key=lambda item: (item.start_ms, item.end_ms)):
                if cue.start_ms > cursor:
                    uncovered.append(TranscriptInterval(start_ms=cursor, end_ms=cue.start_ms))
                cursor = max(cursor, cue.end_ms)
            if cursor < media_manifest.probe.duration_ms:
                uncovered.append(
                    TranscriptInterval(start_ms=cursor, end_ms=media_manifest.probe.duration_ms)
                )
            asr_intervals = tuple(uncovered)
    else:
        coverage = None
        asr_intervals = (TranscriptInterval(start_ms=0, end_ms=media_manifest.probe.duration_ms),)
    if asr_intervals:
        if media_manifest.speech_audio is None:
            raise state_error(
                "TRANSCRIPTION_SOURCE_UNAVAILABLE",
                "captions are insufficient and normalized speech audio is unavailable",
            )
        if backend is None:
            from video_evidence_pipeline.transcription.faster_whisper import FasterWhisperBackend

            backend = FasterWhisperBackend(
                model_directory,
                device=config.asr_device,
                compute_type=config.asr_compute_type,
            )
        selected_artifacts_list.append(media_manifest.speech_audio.artifact)
        asr_backend = backend.identity()
        if asr_backend.source is not TranscriptSource.ASR:
            raise integrity_error("TRANSCRIPT_BACKEND_INVALID", "ASR backend identity is invalid")
        backend_identities.append(asr_backend)
        asr_backend_fingerprint = fingerprint(asr_backend)
        asr_segments: list[AsrSegment] = list(
            backend.transcribe(
                workspace.safe_path(media_manifest.speech_audio.artifact.relative_storage_path),
                language=config.asr_language or job.assignment.language,
                intervals=asr_intervals,
                configuration=config,
            )
        )
        backend_segment_ids = [item.backend_segment_id for item in asr_segments]
        if len(set(backend_segment_ids)) != len(backend_segment_ids):
            raise integrity_error(
                "TRANSCRIPT_BACKEND_INVALID", "ASR backend segment IDs are not unique"
            )
        for segment in asr_segments:
            if not any(
                segment.start_ms >= interval.start_ms and segment.end_ms <= interval.end_ms
                for interval in asr_intervals
            ):
                raise integrity_error(
                    "TRANSCRIPT_BACKEND_INVALID",
                    "ASR returned a segment outside the requested gap",
                )
            raw.append(
                (
                    segment.start_ms,
                    segment.end_ms,
                    segment.raw_text,
                    segment.speaker,
                    TranscriptSource.ASR,
                    segment.confidence,
                    list(segment.flags),
                    SegmentLineage(
                        source_artifact=media_manifest.speech_audio.artifact,
                        source_record_id=segment.backend_segment_id,
                        backend_identity_fingerprint=asr_backend_fingerprint,
                    ),
                )
            )
    segments, gaps, overlaps = _validate_segments(raw, config, media_manifest.probe.duration_ms)
    if gaps:
        warnings.append("TRANSCRIPT_GAPS_RECORDED")
    if overlaps:
        warnings.append("TRANSCRIPT_OVERLAPS_PRESERVED")
    media_fingerprint = media_bundle_fingerprint(media_manifest)
    selected_artifacts = tuple(selected_artifacts_list)
    backends = tuple(backend_identities)
    stage_fingerprint = transcription_fingerprint(
        media_fingerprint=media_fingerprint,
        selected_artifacts=selected_artifacts,
        configuration=config,
        backends=backends,
    )
    return PreparedTranscript(
        segments=segments,
        configuration=config,
        backends=backends,
        selected_source_artifacts=selected_artifacts,
        selected_subtitle_stream_index=selected_stream_index,
        selected_subtitle_provenance=selected_provenance,
        caption_coverage=coverage,
        media_bundle_fingerprint=media_fingerprint,
        transcription_fingerprint=stage_fingerprint,
        duration_ms=media_manifest.probe.duration_ms,
        gaps=gaps,
        overlaps=overlaps,
        warnings=tuple(sorted(set(warnings))),
        started_at=started_at,
    )


def _staging_root(workspace: JobWorkspace) -> Path:
    root = workspace.safe_path("transcript/staging")
    ensure_private_directory(root.parent)
    ensure_private_directory(root)
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)() or root.is_mount():
        raise integrity_error("TRANSCRIPTION_STAGING_INVALID", "transcription staging is invalid")
    return root


def cleanup_stale_attempts(workspace: JobWorkspace) -> None:
    root = _staging_root(workspace)
    for child in root.iterdir():
        if (
            child.parent != root
            or _ATTEMPT_NAME.fullmatch(child.name) is None
            or child.is_symlink()
            or getattr(child, "is_junction", lambda: False)()
            or child.is_mount()
            or not child.is_dir()
        ):
            raise integrity_error(
                "TRANSCRIPTION_STAGING_INVALID", "transcription staging is invalid"
            )
        shutil.rmtree(child)


def create_attempt_directory(workspace: JobWorkspace) -> Path:
    target = _staging_root(workspace) / uuid.uuid4().hex
    ensure_private_directory(target, exist_ok=False)
    return target


def cleanup_attempt(workspace: JobWorkspace, attempt: Path) -> None:
    root = _staging_root(workspace)
    if not attempt.exists():
        return
    if (
        attempt.parent != root
        or _ATTEMPT_NAME.fullmatch(attempt.name) is None
        or attempt.is_symlink()
        or getattr(attempt, "is_junction", lambda: False)()
        or attempt.is_mount()
        or not attempt.is_dir()
    ):
        raise integrity_error("TRANSCRIPTION_STAGING_INVALID", "transcription staging is invalid")
    shutil.rmtree(attempt)


def _verify_artifact(path: Path, artifact: ArtifactRef, *, deep: bool = False) -> None:
    try:
        if not artifact_file_matches(path, artifact, deep=True, hasher=sha256_file):
            raise integrity_error("TRANSCRIPT_BUNDLE_TAMPERED", "transcript artifact is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "TRANSCRIPT_BUNDLE_TAMPERED", "transcript artifact is unavailable"
        ) from None


def _segments_payload(segments: tuple[TranscriptSegment, ...]) -> bytes:
    return b"".join(canonical_json_bytes(item) + b"\n" for item in segments)


def _publish_segments(workspace: JobWorkspace, staged_path: Path, payload: bytes) -> ArtifactRef:
    content_sha256 = sha256_file(staged_path)
    artifact = ArtifactRef(
        artifact_id=f"transcript-{content_sha256[:16]}",
        artifact_type="transcript_segments",
        content_sha256=content_sha256,
        size_bytes=len(payload),
        relative_storage_path=f"transcript/segments/{content_sha256}/transcript.jsonl",
    )
    target = workspace.safe_path(artifact.relative_storage_path)
    ensure_private_directory(target.parent.parent)
    ensure_private_directory(target.parent)
    atomic_copy_exclusive(
        staged_path, target, verifier=lambda item: _verify_artifact(item, artifact, deep=True)
    )
    _verify_artifact(target, artifact, deep=True)
    return artifact


def _stage_manifest(manifest: TranscriptManifest) -> StageManifest:
    return StageManifest(
        job_id=manifest.job_id,
        stage="transcription",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.TRANSCRIBED,
        input_artifacts=list(manifest.selected_source_artifacts),
        output_artifacts=[manifest.segments_artifact],
        producer=ProducerIdentity(
            kind=ProducerKind.ADAPTER,
            name="transcription-pipeline",
            version=__version__,
        ),
        configuration_sha256=manifest.transcription_fingerprint,
        started_at=manifest.started_at,
        finished_at=manifest.created_at,
        warnings=list(manifest.warnings),
    )


def publish_transcript(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    prepared: PreparedTranscript,
    attempt: Path,
) -> TranscriptionResult:
    payload = _segments_payload(prepared.segments)
    staged_path = attempt / "transcript.jsonl"
    atomic_write_bytes(staged_path, payload, private=True)
    staged_artifact = ArtifactRef(
        artifact_id=f"transcript-{sha256_file(staged_path)[:16]}",
        artifact_type="transcript_segments",
        content_sha256=sha256_file(staged_path),
        size_bytes=len(payload),
        relative_storage_path="transcript/segments/"
        + sha256_file(staged_path)
        + "/transcript.jsonl",
    )
    _verify_artifact(staged_path, staged_artifact, deep=True)
    _read_segments(staged_path, prepared.configuration, prepared.duration_ms)
    segments_artifact = _publish_segments(workspace, staged_path, payload)
    created_at = datetime.now(UTC)
    manifest = TranscriptManifest(
        job_id=job.job_id,
        transcription_fingerprint=prepared.transcription_fingerprint,
        media_bundle_fingerprint=prepared.media_bundle_fingerprint,
        implementation_version=TRANSCRIPTION_IMPLEMENTATION_VERSION,
        configuration=prepared.configuration,
        backends=list(prepared.backends),
        selected_source_artifacts=list(prepared.selected_source_artifacts),
        selected_subtitle_stream_index=prepared.selected_subtitle_stream_index,
        selected_subtitle_provenance=prepared.selected_subtitle_provenance,
        caption_coverage=prepared.caption_coverage,
        segments_artifact=segments_artifact,
        segment_count=len(prepared.segments),
        duration_ms=prepared.duration_ms,
        gaps=list(prepared.gaps),
        overlaps=list(prepared.overlaps),
        started_at=prepared.started_at,
        created_at=created_at,
        warnings=list(prepared.warnings),
    )
    stage = _stage_manifest(manifest)
    if workspace.transcript_manifest_json.exists():
        existing = load_transcription_result(workspace, job, media_manifest)
        if (
            existing.transcript_manifest.transcription_fingerprint
            != manifest.transcription_fingerprint
        ):
            raise integrity_error(
                "TRANSCRIPTION_FINGERPRINT_MISMATCH",
                "another transcript bundle uses a different fingerprint",
            )
        return existing
    atomic_write_json(workspace.transcript_candidate_manifest_json, manifest, private=True)
    candidate = _read_bounded_json_model(
        workspace.transcript_candidate_manifest_json,
        TranscriptManifest,
        code="TRANSCRIPT_MANIFEST_INVALID",
    )
    _verify_transcript_manifest(workspace, job, media_manifest, candidate, prepared.configuration)
    atomic_write_json(workspace.transcription_stage_manifest_json, stage, private=True)
    stored_stage = _read_bounded_json_model(
        workspace.transcription_stage_manifest_json,
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    if stored_stage != stage:
        raise integrity_error("STAGE_MANIFEST_MISMATCH", "transcription stage manifest is invalid")
    # The fixed primary manifest is the bundle commit marker and is written last.
    atomic_write_json(workspace.transcript_manifest_json, manifest, private=True)
    return TranscriptionResult(manifest, stage, prepared.segments, reused=False)


def _read_segments(
    path: Path, config: TranscriptionConfig, duration_ms: int
) -> tuple[TranscriptSegment, ...]:
    maximum_bytes = min(
        2_147_483_648,
        config.maximum_total_text_chars * 8 + config.maximum_segments * 2048,
    )
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum_bytes
        ):
            raise integrity_error("TRANSCRIPT_INVALID", "transcript JSONL is invalid")
        payload = path.read_bytes()
    except OSError:
        raise integrity_error("TRANSCRIPT_INVALID", "transcript JSONL is unavailable") from None
    if len(payload) != metadata.st_size or not payload.endswith(b"\n"):
        raise integrity_error("TRANSCRIPT_INVALID", "transcript JSONL is invalid")
    try:
        segments = tuple(
            TranscriptSegment.model_validate_json(line, strict=True)
            for line in payload.splitlines()
        )
    except (UnicodeError, ValidationError):
        raise integrity_error("TRANSCRIPT_INVALID", "transcript JSONL is malformed") from None
    if len(segments) > config.maximum_segments:
        raise integrity_error("TRANSCRIPT_INVALID", "transcript JSONL exceeds its limit")
    ids = [item.segment_id for item in segments]
    if (
        not segments
        or ids != [f"T{index:06d}" for index in range(1, len(ids) + 1)]
        or list(segments)
        != sorted(
            segments,
            key=lambda item: (item.start_ms, item.end_ms, item.lineage.source_record_id),
        )
        or any(item.end_ms > duration_ms for item in segments)
        or any(
            normalize_transcript_text(item.raw_text) != item.normalized_text for item in segments
        )
        or sum(len(item.raw_text) + len(item.normalized_text) for item in segments)
        > config.maximum_total_text_chars
    ):
        raise integrity_error("TRANSCRIPT_INVALID", "transcript JSONL is inconsistent")
    return segments


def _verify_transcript_manifest(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    manifest: TranscriptManifest,
    expected_config: TranscriptionConfig | None,
    *,
    deep: bool = False,
) -> tuple[TranscriptManifest, tuple[TranscriptSegment, ...]]:
    media_fingerprint = media_bundle_fingerprint(media_manifest)
    if (
        manifest.job_id != job.job_id
        or manifest.implementation_version != TRANSCRIPTION_IMPLEMENTATION_VERSION
        or manifest.media_bundle_fingerprint != media_fingerprint
        or (expected_config is not None and manifest.configuration != expected_config)
        or manifest.transcription_fingerprint
        != transcription_fingerprint(
            media_fingerprint=media_fingerprint,
            selected_artifacts=tuple(manifest.selected_source_artifacts),
            configuration=manifest.configuration,
            backends=tuple(manifest.backends),
        )
        or not manifest.segments_artifact.relative_storage_path.startswith("transcript/segments/")
    ):
        raise integrity_error(
            "TRANSCRIPT_MANIFEST_MISMATCH", "transcript manifest does not match the job"
        )
    allowed_sources = [
        *(
            item.cues_artifact
            for item in media_manifest.subtitles
            if item.cues_artifact is not None
        ),
        *(
            [media_manifest.speech_audio.artifact]
            if media_manifest.speech_audio is not None
            else []
        ),
    ]
    if any(item not in allowed_sources for item in manifest.selected_source_artifacts):
        raise integrity_error(
            "TRANSCRIPT_MANIFEST_MISMATCH", "transcript source artifact is unknown"
        )
    segments_path = workspace.safe_path(manifest.segments_artifact.relative_storage_path)
    _verify_artifact(segments_path, manifest.segments_artifact, deep=deep)
    segments = _read_segments(segments_path, manifest.configuration, manifest.duration_ms)
    gaps, overlaps = _intervals(segments, manifest.duration_ms)
    backend_by_fingerprint = {fingerprint(item): item for item in manifest.backends}
    if len(backend_by_fingerprint) != len(manifest.backends):
        raise integrity_error(
            "TRANSCRIPT_MANIFEST_MISMATCH", "transcript backend identities are ambiguous"
        )
    segment_ids = {item.segment_id for item in segments}
    if any(
        source_id not in segment_ids for interval in overlaps for source_id in interval.segment_ids
    ):
        raise integrity_error(
            "TRANSCRIPT_MANIFEST_MISMATCH", "transcript overlap lineage is unknown"
        )
    caption_artifact: ArtifactRef | None = None
    cue_by_id: dict[str, SubtitleCue] = {}
    if manifest.selected_subtitle_stream_index is not None:
        preferred = list(manifest.configuration.preferred_languages)
        if job.assignment.language.casefold() not in {item.casefold() for item in preferred}:
            preferred.append(job.assignment.language)
        expected_candidates = sorted(
            (
                item
                for item in media_manifest.subtitles
                if item.status is SubtitleStatus.NORMALIZED
                and item.cues_artifact is not None
                and item.cue_count > 0
            ),
            key=lambda item: _subtitle_order(item, preferred),
        )
        subtitle = next(
            (
                item
                for item in media_manifest.subtitles
                if item.stream_index == manifest.selected_subtitle_stream_index
                and item.status is SubtitleStatus.NORMALIZED
                and item.cues_artifact is not None
            ),
            None,
        )
        if (
            subtitle is None
            or not expected_candidates
            or subtitle != expected_candidates[0]
            or subtitle.provenance is not manifest.selected_subtitle_provenance
            or subtitle.cues_artifact not in manifest.selected_source_artifacts
        ):
            raise integrity_error(
                "TRANSCRIPT_MANIFEST_MISMATCH", "selected subtitle source is invalid"
            )
        caption_artifact = subtitle.cues_artifact
        cues = verify_cue_jsonl(
            workspace.safe_path(caption_artifact.relative_storage_path),
            maximum_bytes=media_manifest.configuration.maximum_subtitle_bytes,
        )
        cue_by_id = {item.cue_id: item for item in cues}
        if manifest.caption_coverage != _caption_coverage(cues, manifest.duration_ms):
            raise integrity_error(
                "TRANSCRIPT_MANIFEST_MISMATCH", "caption coverage metadata is invalid"
            )
        expected_caption_backend = TranscriptionBackendIdentity(
            source=TranscriptSource.NORMALIZED_SUBTITLE,
            name="normalized-subtitle",
            version=media_manifest.implementation_version,
            model=f"{subtitle.provenance.value}:stream-{subtitle.stream_index}",
            model_fingerprint=caption_artifact.content_sha256,
            local_files_only=True,
        )
        if expected_caption_backend not in manifest.backends:
            raise integrity_error(
                "TRANSCRIPT_MANIFEST_MISMATCH", "caption backend identity is invalid"
            )
    used_sources: list[TranscriptSource] = []
    expected_source_artifacts: list[ArtifactRef] = []
    for source in (TranscriptSource.NORMALIZED_SUBTITLE, TranscriptSource.ASR):
        source_segments = [item for item in segments if item.source is source]
        if not source_segments:
            continue
        used_sources.append(source)
        backend = next((item for item in manifest.backends if item.source is source), None)
        if backend is None:
            raise integrity_error(
                "TRANSCRIPT_MANIFEST_MISMATCH", "segment source lacks a backend identity"
            )
        backend_fingerprint = fingerprint(backend)
        if any(
            item.lineage.backend_identity_fingerprint != backend_fingerprint
            for item in source_segments
        ):
            raise integrity_error(
                "TRANSCRIPT_MANIFEST_MISMATCH", "segment backend lineage is invalid"
            )
        if source is TranscriptSource.NORMALIZED_SUBTITLE:
            if caption_artifact is None:
                raise integrity_error(
                    "TRANSCRIPT_MANIFEST_MISMATCH", "caption segment lacks a selected subtitle"
                )
            expected_source_artifacts.append(caption_artifact)
            for item in source_segments:
                cue = cue_by_id.get(item.lineage.source_record_id)
                if (
                    cue is None
                    or item.lineage.source_artifact != caption_artifact
                    or item.lineage.source_stream_index != manifest.selected_subtitle_stream_index
                    or item.lineage.subtitle_provenance is not manifest.selected_subtitle_provenance
                    or (item.start_ms, item.end_ms, item.raw_text)
                    != (cue.start_ms, cue.end_ms, cue.text)
                ):
                    raise integrity_error(
                        "TRANSCRIPT_MANIFEST_MISMATCH", "caption segment lineage is invalid"
                    )
        else:
            if media_manifest.speech_audio is None:
                raise integrity_error(
                    "TRANSCRIPT_MANIFEST_MISMATCH", "ASR segment lacks normalized speech audio"
                )
            expected_source_artifacts.append(media_manifest.speech_audio.artifact)
            if any(
                item.lineage.source_artifact != media_manifest.speech_audio.artifact
                or item.lineage.source_stream_index is not None
                or item.lineage.subtitle_provenance is not None
                for item in source_segments
            ):
                raise integrity_error(
                    "TRANSCRIPT_MANIFEST_MISMATCH", "ASR segment lineage is invalid"
                )
    if (
        len(segments) != manifest.segment_count
        or gaps != tuple(manifest.gaps)
        or overlaps != tuple(manifest.overlaps)
        or manifest.duration_ms != media_manifest.probe.duration_ms
        or [item.source for item in manifest.backends] != used_sources
        or manifest.selected_source_artifacts != expected_source_artifacts
    ):
        raise integrity_error("TRANSCRIPT_MANIFEST_MISMATCH", "transcript bundle is inconsistent")
    return manifest, segments


def verify_transcript_bundle(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    *,
    expected_config: TranscriptionConfig | None = None,
    deep: bool = False,
) -> tuple[TranscriptManifest, tuple[TranscriptSegment, ...]]:
    manifest = _read_bounded_json_model(
        workspace.transcript_manifest_json,
        TranscriptManifest,
        code="TRANSCRIPT_MANIFEST_INVALID",
    )
    return _verify_transcript_manifest(
        workspace, job, media_manifest, manifest, expected_config, deep=deep
    )


def load_transcription_result(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest | None = None,
    *,
    expected_config: TranscriptionConfig | None = None,
    deep: bool = False,
) -> TranscriptionResult:
    if media_manifest is None:
        raise integrity_error("TRANSCRIPT_CONTEXT_MISSING", "media manifest is required")
    manifest, segments = verify_transcript_bundle(
        workspace, job, media_manifest, expected_config=expected_config, deep=deep
    )
    stage = _read_bounded_json_model(
        workspace.transcription_stage_manifest_json,
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    if stage != _stage_manifest(manifest):
        raise integrity_error("STAGE_MANIFEST_MISMATCH", "transcription stage manifest is invalid")
    if workspace.transcript_candidate_manifest_json.exists():
        candidate = _read_bounded_json_model(
            workspace.transcript_candidate_manifest_json,
            TranscriptManifest,
            code="TRANSCRIPT_MANIFEST_INVALID",
        )
        if candidate != manifest:
            raise integrity_error(
                "TRANSCRIPT_MANIFEST_MISMATCH", "transcript candidate manifest is inconsistent"
            )
    return TranscriptionResult(manifest, stage, segments, reused=True)


def recover_transcription_result(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    *,
    expected_config: TranscriptionConfig | None = None,
) -> TranscriptionResult | None:
    primary_exists = workspace.transcript_manifest_json.exists()
    candidate_exists = workspace.transcript_candidate_manifest_json.exists()
    stage_exists = workspace.transcription_stage_manifest_json.exists()
    if not primary_exists and not candidate_exists:
        if stage_exists:
            raise integrity_error(
                "TRANSCRIPT_MANIFEST_INCOMPLETE",
                "transcription stage cannot be recovered without its candidate manifest",
            )
        return None
    if primary_exists:
        manifest, _segments = verify_transcript_bundle(
            workspace, job, media_manifest, expected_config=expected_config
        )
    else:
        manifest = _read_bounded_json_model(
            workspace.transcript_candidate_manifest_json,
            TranscriptManifest,
            code="TRANSCRIPT_MANIFEST_INVALID",
        )
        _verify_transcript_manifest(workspace, job, media_manifest, manifest, expected_config)
    if not stage_exists:
        atomic_write_json(
            workspace.transcription_stage_manifest_json,
            _stage_manifest(manifest),
            private=True,
        )
    else:
        stage = _read_bounded_json_model(
            workspace.transcription_stage_manifest_json,
            StageManifest,
            code="STAGE_MANIFEST_INVALID",
        )
        if stage != _stage_manifest(manifest):
            raise integrity_error(
                "STAGE_MANIFEST_MISMATCH", "transcription stage manifest is invalid"
            )
    if not primary_exists:
        atomic_write_json(workspace.transcript_manifest_json, manifest, private=True)
    return load_transcription_result(
        workspace,
        job,
        media_manifest,
        expected_config=expected_config,
    )
