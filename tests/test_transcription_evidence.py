from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_media_normalization import _synthetic_source
from typer.testing import CliRunner

import video_evidence_pipeline.application as application_module
import video_evidence_pipeline.transcription.container as container_module
import video_evidence_pipeline.transcription.faster_whisper as faster_whisper_module
import video_evidence_pipeline.transcription.provisioning as provisioning_module
from video_evidence_pipeline.application import (
    _import_evidence_fixture,
    acquire_job,
    authorize_job,
    build_search_index_job,
    import_evidence_job,
    initialize_job,
    job_status,
    normalize_job,
    prepare_evidence_job,
    search_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRef,
    EvidenceLedgerArtifactRef,
    artifact_file_matches,
)
from video_evidence_pipeline.domain.evidence import (
    AmbiguityRecord,
    AmbiguityStatus,
    AnalyzerExecutionMode,
    AnalyzerIdentity,
    EvidenceConfidence,
    EvidenceManifest,
    EvidenceRecord,
    EvidenceResponseManifest,
    InterpretationLevel,
    ReviewStatus,
    SourceRef,
    SourceRefKind,
)
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    SourceKind,
    SourceSpec,
)
from video_evidence_pipeline.domain.media import (
    AnalysisProxy,
    FrameIndex,
    FrameReason,
    FrameRecord,
    MediaManifest,
    MediaProbe,
    MediaRational,
    MediaStream,
    MediaStreamType,
    NormalizationConfig,
    NormalizedSubtitle,
    SpeechAudio,
    SubtitleCue,
    SubtitleKind,
    SubtitleProvenance,
    SubtitleStatus,
    ToolIdentity,
)
from video_evidence_pipeline.domain.serialization import (
    canonical_json_bytes,
    fingerprint,
    pretty_json_bytes,
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
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.evidence.pipeline import (
    create_attempt_directory as create_evidence_attempt,
)
from video_evidence_pipeline.evidence.pipeline import (
    load_evidence_result,
    prepare_evidence_request,
    publish_evidence,
    read_evidence_response,
    recover_evidence_result,
)
from video_evidence_pipeline.storage.jsonl import exclusive_append_lock, read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.transcription.device import (
    GpuDevice,
    RuntimeCapabilities,
)
from video_evidence_pipeline.transcription.faster_whisper import FasterWhisperBackend
from video_evidence_pipeline.transcription.pipeline import (
    TranscriptionResult,
    _intervals,
    backend_identity_fingerprint,
    load_transcription_result,
    prepare_transcript,
    publish_transcript,
    recover_transcription_result,
)
from video_evidence_pipeline.transcription.pipeline import (
    create_attempt_directory as create_transcript_attempt,
)
from video_evidence_pipeline.transcription.profiles import (
    DEFAULT_ASR_PROFILE,
    AsrModelProvisionManifest,
    resolve_asr_profile,
)

_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_RAW_SPACED_TEXT = "  " + chr(0xFF21) + "   B  "


def _artifact(
    workspace: JobWorkspace,
    relative_path: str,
    payload: bytes,
    *,
    artifact_id: str,
    artifact_type: str,
) -> ArtifactRef:
    path = workspace.safe_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactRef(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        content_sha256=sha256_bytes(payload),
        size_bytes=len(payload),
        relative_storage_path=relative_path,
    )


def _job(job_id: str = "issue14-job") -> JobSpec:
    return JobSpec(
        job_id=job_id,
        created_at=_NOW,
        source=SourceSpec(kind=SourceKind.LOCAL_FILE, locator="synthetic.mkv"),
        assignment=AssignmentSpec(
            task_type="report",
            language="ja",
            requirements=["Identify the key event", "Preserve unresolved uncertainty"],
        ),
        output=OutputSpec(format="markdown"),
    )


def _workspace(tmp_path: Path, job: JobSpec) -> JobWorkspace:
    return JobWorkspace.create(tmp_path, job.job_id)


def _required_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg
    if os.environ.get("VEP_REQUIRE_FFMPEG_TESTS") == "1":
        pytest.fail("VEP_REQUIRE_FFMPEG_TESTS=1 but ffmpeg/ffprobe are unavailable")
    pytest.skip("ffmpeg/ffprobe unavailable; application integration was not run")


def _synthetic_captioned_source(root: Path) -> Path:
    ffmpeg = _required_ffmpeg()
    subtitle = root / "application.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\napplication evidence\n",
        encoding="utf-8",
    )
    source = root / "application.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=5:duration=3",
            "-i",
            str(subtitle),
            "-map",
            "0:v:0",
            "-map",
            "1:s:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:s",
            "subrip",
            "-t",
            "3",
            "-y",
            str(source),
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return source


def _application_transcribed_job(
    tmp_path: Path,
    job_id: str,
    *,
    min_chars: int | None = None,
    max_chars: int | None = None,
    maximum_length_adjustments: int = 3,
) -> tuple[JobWorkspace, TranscriptionResult]:
    repository = tmp_path / "r"
    repository.mkdir()
    source = _synthetic_captioned_source(repository)
    workspace, _job_value = initialize_job(
        repository,
        job_id=job_id,
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="en",
        output_format="markdown",
        requirements=["Identify the key event", "Preserve unresolved uncertainty"],
        min_chars=min_chars,
        max_chars=max_chars,
        maximum_length_adjustments=maximum_length_adjustments,
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic Issue 14 fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)
    transcription = transcribe_job(
        workspace.root,
        TranscriptionConfig(preferred_languages=["en"], minimum_caption_coverage=0.8),
    )
    return workspace, transcription


def _media_manifest(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    subtitles: list[
        tuple[
            str | None,
            SubtitleProvenance,
            bool,
            bool,
            list[SubtitleCue],
        ]
    ]
    | None = None,
    audio: bool = False,
    duration_ms: int = 4000,
) -> MediaManifest:
    config = NormalizationConfig()
    raw = _artifact(
        workspace,
        "source/raw/raw.bin",
        b"raw",
        artifact_id="raw-source",
        artifact_type="raw_source",
    )
    proxy = _artifact(
        workspace,
        "normalized/proxy/analysis.mp4",
        b"proxy",
        artifact_id="analysis-proxy",
        artifact_type="analysis_proxy",
    )
    streams = [
        MediaStream(
            index=0,
            stream_type=MediaStreamType.VIDEO,
            codec_name="h264",
            time_base=MediaRational(numerator=1, denominator=1000),
            frame_rate=MediaRational(numerator=25, denominator=1),
            duration_ms=duration_ms,
            width=320,
            height=180,
        )
    ]
    speech_audio = None
    next_stream_index = 1
    if audio:
        streams.append(
            MediaStream(
                index=1,
                stream_type=MediaStreamType.AUDIO,
                codec_name="aac",
                time_base=MediaRational(numerator=1, denominator=1000),
                duration_ms=duration_ms,
                sample_rate_hz=48_000,
                channels=2,
            )
        )
        audio_artifact = _artifact(
            workspace,
            "normalized/audio/speech.wav",
            b"synthetic-wave",
            artifact_id="speech-audio",
            artifact_type="normalized_speech_audio",
        )
        speech_audio = SpeechAudio(
            artifact=audio_artifact,
            format_name="wav",
            codec_name="pcm_s16le",
            sample_rate_hz=config.speech_sample_rate_hz,
            channels=1,
            duration_ms=duration_ms,
        )
        next_stream_index = 2

    normalized_subtitles: list[NormalizedSubtitle] = []
    for offset, (language, provenance, is_default, is_forced, cues) in enumerate(subtitles or []):
        stream_index = next_stream_index + offset
        streams.append(
            MediaStream(
                index=stream_index,
                stream_type=MediaStreamType.SUBTITLE,
                codec_name="subrip",
                time_base=MediaRational(numerator=1, denominator=1000),
                duration_ms=duration_ms,
                language=language,
                subtitle_kind=SubtitleKind.TEXT,
                subtitle_provenance=provenance,
                is_default=is_default,
                is_forced=is_forced,
            )
        )
        cue_payload = b"".join(canonical_json_bytes(item) + b"\n" for item in cues) or b"\n"
        cues_artifact = _artifact(
            workspace,
            f"normalized/subtitles/{stream_index}/cues.jsonl",
            cue_payload,
            artifact_id=f"subtitle-cues-{stream_index}",
            artifact_type="normalized_subtitle_cues",
        )
        webvtt_artifact = _artifact(
            workspace,
            f"normalized/subtitles/{stream_index}/subtitle.vtt",
            b"WEBVTT\n\n",
            artifact_id=f"subtitle-vtt-{stream_index}",
            artifact_type="normalized_subtitle_webvtt",
        )
        normalized_subtitles.append(
            NormalizedSubtitle(
                stream_index=stream_index,
                codec_name="subrip",
                language=language,
                kind=SubtitleKind.TEXT,
                provenance=provenance,
                is_default=is_default,
                is_forced=is_forced,
                status=SubtitleStatus.NORMALIZED,
                webvtt_artifact=webvtt_artifact,
                cues_artifact=cues_artifact,
                cue_count=len(cues),
            )
        )

    frame = _artifact(
        workspace,
        "normalized/frames/f1.png",
        b"frame",
        artifact_id="frame-1",
        artifact_type="extracted_frame",
    )
    frame_index = FrameIndex(
        duration_ms=duration_ms,
        records=[
            FrameRecord(
                frame_id="F000001",
                timestamp_ms=500,
                artifact=frame,
                perceptual_hash="0" * 16,
                reasons=[FrameReason.PERIODIC],
                candidate_timestamps_ms=[500],
            )
        ],
        candidate_count=1,
        duplicate_count=0,
    )
    frame_index_artifact = _artifact(
        workspace,
        "normalized/frames/index.json",
        canonical_json_bytes(frame_index),
        artifact_id="frame-index",
        artifact_type="frame_index",
    )
    tool = ToolIdentity(
        name="ffmpeg",
        version="synthetic-1",
        executable_sha256="a" * 64,
        capabilities=[],
    )
    probe_tool = ToolIdentity(
        name="ffprobe",
        version="synthetic-1",
        executable_sha256="b" * 64,
        capabilities=[],
    )
    return MediaManifest(
        job_id=job.job_id,
        normalization_fingerprint="c" * 64,
        implementation_version="normalization-test-v1",
        configuration=config,
        ffmpeg=tool,
        ffprobe=probe_tool,
        raw_artifact=raw,
        probe=MediaProbe(format_name="matroska", duration_ms=duration_ms, streams=streams),
        analysis_proxy=AnalysisProxy(
            artifact=proxy,
            format_name="mov,mp4",
            codec_name="h264",
            width=config.analysis_width,
            height=config.analysis_height,
            duration_ms=duration_ms,
        ),
        speech_audio=speech_audio,
        subtitles=normalized_subtitles,
        frame_index_artifact=frame_index_artifact,
        frame_index=frame_index,
        warnings=[] if audio else ["AUDIO_STREAM_UNAVAILABLE"],
        started_at=_NOW,
        created_at=_NOW,
    )


def _asr_confidence() -> TranscriptConfidence:
    return TranscriptConfidence(
        kind=TranscriptConfidenceKind.BACKEND_METRIC,
        avg_logprob=-0.25,
        no_speech_probability=0.1,
    )


class _FakeAsr:
    def __init__(self, segments: list[AsrSegment] | None = None) -> None:
        self.segments = segments
        self.calls: list[tuple[tuple[int, int], ...]] = []
        self.languages: list[str | None] = []

    def identity(self) -> TranscriptionBackendIdentity:
        return TranscriptionBackendIdentity(
            source=TranscriptSource.ASR,
            name="injected-test-asr",
            version="1",
            runtime="test-runtime",
            model="test-model",
            model_fingerprint="d" * 64,
            local_files_only=True,
        )

    def transcribe(self, audio_path: Path, *, language, intervals, configuration):
        del audio_path, configuration
        self.languages.append(language)
        self.calls.append(tuple((item.start_ms, item.end_ms) for item in intervals))
        if self.segments is not None:
            return list(self.segments)
        return [
            AsrSegment(
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                raw_text=f"  ASR {index}  ",
                backend_segment_id=f"asr-{index}",
                confidence=_asr_confidence(),
            )
            for index, item in enumerate(intervals, start=1)
        ]


class _InferenceFallbackAsr(_FakeAsr):
    def __init__(self) -> None:
        super().__init__()
        self.fell_back = False

    def identity(self) -> TranscriptionBackendIdentity:
        return TranscriptionBackendIdentity(
            source=TranscriptSource.ASR,
            name="injected-fallback-asr",
            version="1",
            runtime=("test-runtime;device=cpu" if self.fell_back else "test-runtime;device=cuda"),
            model="test-model",
            model_fingerprint="d" * 64,
            device_policy="auto",
            selected_device="cpu" if self.fell_back else "cuda",
            gpu_index=0,
            gpu_name="synthetic-gpu",
            total_vram_mib=8192,
            free_vram_at_selection_mib=6144,
            configured_min_free_vram_mib=6144,
            compute_type="int8" if self.fell_back else "float16",
            fallback=self.fell_back,
            fallback_from="cuda" if self.fell_back else None,
            fallback_reason="CUDA out of memory" if self.fell_back else None,
            local_files_only=True,
        )

    def transcribe(self, audio_path: Path, *, language, intervals, configuration):
        segments = super().transcribe(
            audio_path,
            language=language,
            intervals=intervals,
            configuration=configuration,
        )
        self.fell_back = True
        return segments


def _full_caption(text: str = _RAW_SPACED_TEXT) -> list[SubtitleCue]:
    return [SubtitleCue(cue_id="cue-1", start_ms=0, end_ms=4000, text=text)]


def test_subtitle_only_preserves_raw_and_needs_no_asr_or_model(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(
        workspace,
        job,
        subtitles=[("ja", SubtitleProvenance.EMBEDDED, True, False, _full_caption())],
    )

    prepared = prepare_transcript(
        workspace,
        job,
        media,
        TranscriptionConfig(preferred_languages=["ja"]),
        backend=None,
        model_directory=None,
    )

    assert [item.source for item in prepared.segments] == [TranscriptSource.NORMALIZED_SUBTITLE]
    assert prepared.segments[0].raw_text == _RAW_SPACED_TEXT
    assert prepared.segments[0].normalized_text == "A B"
    assert prepared.segments[0].lineage.subtitle_provenance is SubtitleProvenance.EMBEDDED
    assert prepared.caption_coverage == 1.0
    assert prepared.gaps == ()
    assert [item.source for item in prepared.backends] == [TranscriptSource.NORMALIZED_SUBTITLE]


def test_public_transcribe_full_caption_does_not_probe_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "caption-cli"
    repository.mkdir()
    source = _synthetic_captioned_source(repository)
    workspace, _job_value = initialize_job(
        repository,
        job_id="caption-without-docker",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="en",
        output_format="markdown",
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic caption-only fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)
    monkeypatch.setattr(
        container_module,
        "_docker",
        lambda: pytest.fail("caption-only transcription must not probe Docker"),
    )

    result = CliRunner().invoke(app, ["transcribe", str(workspace.root), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["state"] == JobState.TRANSCRIBED.value
    assert not repository.joinpath(".local/models").exists()


def test_public_transcribe_asr_required_reports_missing_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "asr-cli"
    repository.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "config", repository / "config")
    source = _synthetic_source(repository, audio=True, subtitle=False)
    workspace, _job_value = initialize_job(
        repository,
        job_id="asr-without-docker",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="en",
        output_format="markdown",
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic ASR fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)
    original_which = container_module.shutil.which
    monkeypatch.setattr(
        container_module.shutil,
        "which",
        lambda executable: None if executable == "docker" else original_which(executable),
    )

    result = CliRunner().invoke(app, ["transcribe", str(workspace.root), "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "ASR_CONTAINER_RUNTIME_UNAVAILABLE"


def test_subtitle_selection_is_language_then_provenance_default_forced_and_index(
    tmp_path: Path,
) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(
        workspace,
        job,
        subtitles=[
            ("en", SubtitleProvenance.EMBEDDED, True, False, _full_caption("embedded-en")),
            ("ja", SubtitleProvenance.EMBEDDED, True, False, _full_caption("embedded-ja")),
            ("ja", SubtitleProvenance.SIDECAR, True, False, _full_caption("sidecar-ja")),
            ("ja", SubtitleProvenance.OFFICIAL, False, False, _full_caption("official-ja")),
        ],
    )

    japanese = prepare_transcript(
        workspace,
        job,
        media,
        TranscriptionConfig(preferred_languages=["ja", "en"]),
        backend=None,
    )
    english = prepare_transcript(
        workspace,
        job,
        media,
        TranscriptionConfig(preferred_languages=["en", "ja"]),
        backend=None,
    )

    assert japanese.segments[0].raw_text == "official-ja"
    assert japanese.selected_subtitle_provenance is SubtitleProvenance.OFFICIAL
    assert english.segments[0].raw_text == "embedded-en"
    assert english.selected_subtitle_provenance is SubtitleProvenance.EMBEDDED


def test_overlap_sweep_is_bounded_and_rejects_more_than_64_active_segments() -> None:
    artifact = ArtifactRef(
        artifact_id="asr-audio",
        artifact_type="normalized_speech_audio",
        content_sha256="9" * 64,
        size_bytes=1,
        relative_storage_path="normalized/audio/speech.wav",
    )
    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.ASR,
        name="bounded-asr",
        version="1",
        model="local",
        model_fingerprint="8" * 64,
        local_files_only=True,
    )

    def segment(index: int, start: int, end: int) -> TranscriptSegment:
        return TranscriptSegment(
            segment_id=f"T{index:06d}",
            start_ms=start,
            end_ms=end,
            raw_text=str(index),
            normalized_text=str(index),
            source=TranscriptSource.ASR,
            confidence=_asr_confidence(),
            lineage=SegmentLineage(
                source_artifact=artifact,
                source_record_id=str(index),
                backend_identity_fingerprint=fingerprint(backend),
            ),
        )

    sequential = tuple(segment(index, index - 1, index) for index in range(1, 10_001))
    gaps, overlaps = _intervals(sequential, 10_000)
    assert gaps == ()
    assert overlaps == ()

    too_many = tuple(segment(index, 0, 100) for index in range(1, 66))
    with pytest.raises(VepError) as captured:
        _intervals(too_many, 100)
    assert captured.value.code == "TRANSCRIPT_OVERLAP_TOO_COMPLEX"


def test_hybrid_asr_is_invoked_only_for_caption_gaps(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    cues = [SubtitleCue(cue_id="cue-1", start_ms=1000, end_ms=3000, text="caption")]
    media = _media_manifest(
        workspace,
        job,
        subtitles=[("ja", SubtitleProvenance.OFFICIAL, True, False, cues)],
        audio=True,
    )
    backend = _FakeAsr()

    prepared = prepare_transcript(
        workspace,
        job,
        media,
        TranscriptionConfig(minimum_caption_coverage=0.8),
        backend,
    )

    assert backend.calls == [((0, 1000), (3000, 4000))]
    assert [item.source for item in prepared.segments] == [
        TranscriptSource.ASR,
        TranscriptSource.NORMALIZED_SUBTITLE,
        TranscriptSource.ASR,
    ]
    assert [item.segment_id for item in prepared.segments] == [
        "T000001",
        "T000002",
        "T000003",
    ]
    assert prepared.gaps == ()
    assert [item.source for item in prepared.backends] == [
        TranscriptSource.NORMALIZED_SUBTITLE,
        TranscriptSource.ASR,
    ]


def test_zero_cue_subtitle_is_not_claimed_as_a_transcript_source(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(
        workspace,
        job,
        subtitles=[("ja", SubtitleProvenance.EMBEDDED, True, False, [])],
        audio=True,
    )
    backend = _FakeAsr()

    prepared = prepare_transcript(
        workspace,
        job,
        media,
        TranscriptionConfig(),
        backend,
    )

    assert backend.calls == [((0, 4000),)]
    assert backend.languages == [None]
    assert prepared.selected_subtitle_stream_index is None
    assert prepared.selected_subtitle_provenance is None
    assert [item.source for item in prepared.backends] == [TranscriptSource.ASR]


def test_asr_language_is_explicit_source_config_not_assignment_output_language(
    tmp_path: Path,
) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(workspace, job, audio=True)
    backend = _FakeAsr()

    prepare_transcript(
        workspace,
        job,
        media,
        TranscriptionConfig(asr_language="en"),
        backend,
    )

    assert job.assignment.language == "ja"
    assert backend.languages == ["en"]


def test_profiled_asr_manifest_rejects_legacy_incomplete_backend(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(workspace, job, audio=True)
    prepared = prepare_transcript(workspace, job, media, TranscriptionConfig(), _FakeAsr())
    payload = _published_manifest_stub(job, prepared).model_dump(mode="json")
    payload["configuration"]["asr_profile"] = DEFAULT_ASR_PROFILE

    with pytest.raises(ValidationError):
        TranscriptManifest.model_validate(payload)

    pre_46 = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "legacy-transcription"
            / "asr"
            / "transcript-manifest.json"
        ).read_bytes()
    )
    pre_46["configuration"]["asr_profile"] = DEFAULT_ASR_PROFILE
    with pytest.raises(ValidationError):
        TranscriptManifest.model_validate(pre_46)


def test_pipeline_refreshes_backend_identity_after_inference_fallback(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(workspace, job, audio=True)
    backend = _InferenceFallbackAsr()

    prepared = prepare_transcript(workspace, job, media, TranscriptionConfig(), backend)

    assert prepared.backends[0].selected_device == "cpu"
    assert prepared.backends[0].fallback is True
    assert prepared.backends[0].fallback_reason == "CUDA out of memory"
    assert prepared.segments[0].lineage.backend_identity_fingerprint == (
        backend_identity_fingerprint(prepared.backends[0])
    )


def test_asr_duplicate_ids_and_gap_overlap_policies_fail_closed(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(workspace, job, audio=True)
    duplicate = _FakeAsr(
        [
            AsrSegment(
                start_ms=0,
                end_ms=2000,
                raw_text="one",
                backend_segment_id="duplicate",
                confidence=_asr_confidence(),
            ),
            AsrSegment(
                start_ms=2000,
                end_ms=4000,
                raw_text="two",
                backend_segment_id="duplicate",
                confidence=_asr_confidence(),
            ),
        ]
    )
    with pytest.raises(VepError, match="ASR backend segment IDs") as duplicate_error:
        prepare_transcript(workspace, job, media, TranscriptionConfig(), duplicate)
    assert duplicate_error.value.code == "TRANSCRIPT_BACKEND_INVALID"

    gap_backend = _FakeAsr(
        [
            AsrSegment(
                start_ms=1000,
                end_ms=3000,
                raw_text="partial",
                backend_segment_id="partial",
                confidence=_asr_confidence(),
            )
        ]
    )
    with pytest.raises(VepError) as gap_error:
        prepare_transcript(
            workspace,
            job,
            media,
            TranscriptionConfig(gap_policy=GapPolicy.REJECT),
            gap_backend,
        )
    assert gap_error.value.code == "TRANSCRIPT_GAP_REJECTED"

    overlap_backend = _FakeAsr(
        [
            AsrSegment(
                start_ms=0,
                end_ms=3000,
                raw_text="one",
                backend_segment_id="one",
                confidence=_asr_confidence(),
            ),
            AsrSegment(
                start_ms=2000,
                end_ms=4000,
                raw_text="two",
                backend_segment_id="two",
                confidence=_asr_confidence(),
            ),
        ]
    )
    with pytest.raises(VepError) as overlap_error:
        prepare_transcript(
            workspace,
            job,
            media,
            TranscriptionConfig(overlap_policy=OverlapPolicy.REJECT),
            overlap_backend,
        )
    assert overlap_error.value.code == "TRANSCRIPT_OVERLAP_REJECTED"


def test_nested_overlap_intervals_preserve_every_active_segment() -> None:
    artifact = ArtifactRef(
        artifact_id="subtitle-cues",
        artifact_type="normalized_subtitle_cues",
        content_sha256="a" * 64,
        size_bytes=1,
        relative_storage_path="normalized/subtitles/1/cues.jsonl",
    )
    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.NORMALIZED_SUBTITLE,
        name="normalized-subtitle",
        version="1",
        model="embedded:stream-1",
        model_fingerprint="a" * 64,
        local_files_only=True,
    )

    def segment(identifier: str, start: int, end: int) -> TranscriptSegment:
        return TranscriptSegment(
            segment_id=identifier,
            start_ms=start,
            end_ms=end,
            raw_text=identifier,
            normalized_text=identifier,
            source=TranscriptSource.NORMALIZED_SUBTITLE,
            confidence=TranscriptConfidence(kind=TranscriptConfidenceKind.NOT_AVAILABLE),
            lineage=SegmentLineage(
                source_artifact=artifact,
                source_stream_index=1,
                subtitle_provenance=SubtitleProvenance.EMBEDDED,
                source_record_id=identifier,
                backend_identity_fingerprint=fingerprint(backend),
            ),
        )

    gaps, overlaps = _intervals(
        (
            segment("T000001", 0, 1000),
            segment("T000002", 200, 800),
            segment("T000003", 500, 900),
        ),
        1000,
    )

    assert gaps == ()
    assert [(item.start_ms, item.end_ms, item.segment_ids) for item in overlaps] == [
        (200, 500, ["T000001", "T000002"]),
        (500, 800, ["T000001", "T000002", "T000003"]),
        (800, 900, ["T000001", "T000003"]),
    ]


def test_faster_whisper_requires_complete_explicit_local_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(VepError) as missing_path:
        FasterWhisperBackend(None).identity()
    assert missing_path.value.code == "MODEL_UNAVAILABLE"

    model = tmp_path.resolve() / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.bin").write_bytes(b"model")
    with pytest.raises(VepError) as missing_tokenizer:
        FasterWhisperBackend(model).identity()
    assert missing_tokenizer.value.code == "MODEL_UNAVAILABLE"

    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(VepError) as missing_package:
        FasterWhisperBackend(model).identity()
    assert missing_package.value.code == "BACKEND_UNAVAILABLE"


def test_faster_whisper_bounds_model_traversal_before_sorting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path.resolve() / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.bin").write_bytes(b"model")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(faster_whisper_module, "_MAXIMUM_MODEL_FILES", 2)

    with pytest.raises(VepError) as too_many:
        FasterWhisperBackend(model).identity()

    assert too_many.value.code == "MODEL_INVALID"


def _install_fake_faster_whisper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cuda_count: int,
    supported: dict[str, set[str]],
    cuda_supported_by_index: dict[int, set[str]] | None = None,
    fail_devices: set[str] | None = None,
    transcribe_fail_devices: set[str] | None = None,
    failure_message: str = "synthetic model construction failure",
    engine_version: str = "test",
    runtime_version: str = "test",
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class FakeWhisperModel:
        def __init__(self, _path: str, **kwargs: object) -> None:
            calls.append(kwargs)
            self.device = str(kwargs["device"])
            if kwargs["device"] in (fail_devices or set()):
                raise RuntimeError(failure_message)

        def transcribe(self, _path: str, **_kwargs: object):
            if self.device in (transcribe_fail_devices or set()):
                raise RuntimeError(failure_message)
            return iter(()), object()

    runtime = types.ModuleType("ctranslate2")
    runtime.get_cuda_device_count = lambda: cuda_count  # type: ignore[attr-defined]
    runtime.get_supported_compute_types = (  # type: ignore[attr-defined]
        lambda device, device_index=0: (
            (cuda_supported_by_index or {}).get(device_index, supported[device])
            if device == "cuda"
            else supported[device]
        )
    )
    whisper = types.ModuleType("faster_whisper")
    whisper.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ctranslate2", runtime)
    monkeypatch.setitem(sys.modules, "faster_whisper", whisper)
    monkeypatch.setattr(
        faster_whisper_module.importlib.metadata,
        "version",
        lambda name: engine_version if name == "faster-whisper" else runtime_version,
    )
    return calls


def _complete_model(path: Path) -> Path:
    model = path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.bin").write_bytes(b"model")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    return model.resolve()


def test_faster_whisper_auto_resolves_usable_cuda_and_records_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16", "int8"}, "cpu": {"int8"}},
    )

    identity = FasterWhisperBackend(_complete_model(tmp_path)).identity()

    assert calls == [
        {
            "device": "cuda",
            "compute_type": "float16",
            "local_files_only": True,
            "device_index": 0,
        }
    ]
    assert identity.runtime == ("ctranslate2-test;device=cuda;compute_type=float16;device_index=0")
    assert identity.gpu_index == 0


def test_faster_whisper_auto_falls_back_to_cpu_when_cuda_model_load_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16", "int8"}, "cpu": {"int8"}},
        fail_devices={"cuda"},
        failure_message="CUDA out of memory",
    )

    identity = FasterWhisperBackend(_complete_model(tmp_path)).identity()

    assert [call["device"] for call in calls] == ["cuda", "cpu"]
    assert calls[1] == {
        "device": "cpu",
        "compute_type": "int8",
        "local_files_only": True,
    }
    assert identity.runtime == "ctranslate2-test;device=cpu;compute_type=int8;device_index=none"
    assert identity.fallback is True
    assert identity.fallback_from == "cuda"


def test_profiled_inference_gpu_oom_falls_back_once_and_records_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16"}, "cpu": {"int8"}},
        transcribe_fail_devices={"cuda"},
        failure_message="CUDA out of memory",
        engine_version="1.2.1",
        runtime_version="4.8.1",
    )
    _profile_id, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)
    backend = FasterWhisperBackend(
        _complete_model(tmp_path),
        profile=profile,
        container_image=profile.container_image,
        container_image_digest="vep-asr@sha256:" + "a" * 64,
        capability_override=RuntimeCapabilities(
            gpus=(GpuDevice(0, "synthetic-gpu", 8192, 6144),),
            container_gpu_passthrough=True,
            ctranslate2_cuda_available=True,
            cuda_compute_types=frozenset({"float16"}),
        ),
    )

    assert (
        backend.transcribe(
            tmp_path / "audio.wav",
            language=None,
            intervals=(TranscriptInterval(start_ms=0, end_ms=1000),),
            configuration=TranscriptionConfig(),
        )
        == []
    )
    identity = backend.identity()
    assert [call["device"] for call in calls] == ["cuda", "cpu"]
    assert identity.model == profile.model_repository
    assert identity.model_revision == profile.model_revision
    assert identity.device_policy == "auto"
    assert identity.selected_device == "cpu"
    assert identity.gpu_index is None
    assert identity.gpu_name is None
    assert identity.candidate_gpu_index == 0
    assert identity.candidate_gpu_name == "synthetic-gpu"
    assert identity.free_vram_at_selection_mib is None
    assert identity.candidate_free_vram_mib == 6144
    assert identity.compute_type == "int8"
    assert identity.container_image == profile.container_image
    assert identity.fallback is True
    assert identity.selection_reason == "gpu_resource_fallback"
    assert identity.fallback_from == "cuda"
    assert identity.fallback_reason == "CUDA out of memory"


def test_profiled_runtime_filters_compute_support_per_gpu_before_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=2,
        supported={"cuda": {"float16"}, "cpu": {"int8"}},
        cuda_supported_by_index={0: {"int8"}, 1: {"float16"}},
        engine_version="1.2.1",
        runtime_version="4.8.1",
    )
    monkeypatch.setattr(
        faster_whisper_module,
        "probe_nvidia_gpus",
        lambda: (
            GpuDevice(0, "unsupported-gpu", 16384, 12000),
            GpuDevice(1, "supported-gpu", 8192, 7000),
        ),
    )
    _profile_id, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)

    identity = FasterWhisperBackend(_complete_model(tmp_path), profile=profile).identity()

    assert calls[0]["device"] == "cuda"
    assert calls[0]["device_index"] == 1
    assert identity.gpu_index == 1


def test_profiled_explicit_cpu_skips_gpu_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16"}, "cpu": {"int8"}},
        engine_version="1.2.1",
        runtime_version="4.8.1",
    )
    probes = 0

    def probe() -> tuple[GpuDevice, ...]:
        nonlocal probes
        probes += 1
        return (GpuDevice(0, "gpu", 8192, 7000),)

    monkeypatch.setattr(faster_whisper_module, "probe_nvidia_gpus", probe)
    _profile_id, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)
    identity = FasterWhisperBackend(
        _complete_model(tmp_path),
        device="cpu",
        profile=profile,
    ).identity()
    assert probes == 0
    assert identity.selected_device == "cpu"
    assert identity.selection_reason == "explicit_cpu"
    assert identity.gpu_name is None
    assert identity.candidate_gpu_name is None


def test_profiled_runtime_rejects_installed_version_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=0,
        supported={"cuda": set(), "cpu": {"int8"}},
        engine_version="9.9.9",
        runtime_version="4.8.1",
    )
    _profile_id, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)

    with pytest.raises(VepError) as mismatch:
        FasterWhisperBackend(_complete_model(tmp_path), profile=profile).identity()

    assert mismatch.value.code == "ASR_RUNTIME_VERSION_MISMATCH"


def test_profiled_runtime_rejects_model_bytes_that_do_not_match_provision_manifest(
    tmp_path: Path,
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    _profile_id, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)
    provision = AsrModelProvisionManifest(
        profile=DEFAULT_ASR_PROFILE,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity="b" * 64,
        file_count=3,
        total_size_bytes=9,
        provisioned_at=_NOW,
    )

    with pytest.raises(VepError) as mismatch:
        FasterWhisperBackend(
            _complete_model(tmp_path),
            profile=profile,
            provision_manifest=provision,
        ).identity()

    assert mismatch.value.code == "MODEL_INVALID"


def test_profiled_inference_unrelated_failure_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16"}, "cpu": {"int8"}},
        transcribe_fail_devices={"cuda"},
        failure_message="corrupted input",
        engine_version="1.2.1",
        runtime_version="4.8.1",
    )
    _profile_id, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)
    backend = FasterWhisperBackend(
        _complete_model(tmp_path),
        profile=profile,
        capability_override=RuntimeCapabilities(
            gpus=(GpuDevice(0, "synthetic-gpu", 8192, 7000),),
            container_gpu_passthrough=True,
            ctranslate2_cuda_available=True,
            cuda_compute_types=frozenset({"float16"}),
        ),
    )

    with pytest.raises(VepError) as failure:
        backend.transcribe(
            tmp_path / "audio.wav",
            language=None,
            intervals=(TranscriptInterval(start_ms=0, end_ms=1000),),
            configuration=TranscriptionConfig(),
        )

    assert failure.value.code == "TRANSCRIPTION_FAILED"
    assert [call["device"] for call in calls] == ["cuda"]


def test_faster_whisper_explicit_cuda_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=0,
        supported={"cuda": set(), "cpu": {"int8"}},
    )

    with pytest.raises(VepError) as unavailable:
        FasterWhisperBackend(_complete_model(tmp_path), device="cuda").identity()

    assert unavailable.value.code == "ASR_DEVICE_UNAVAILABLE"
    assert calls == []


def test_faster_whisper_explicit_cuda_model_load_failure_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16"}, "cpu": {"int8"}},
        fail_devices={"cuda"},
    )

    with pytest.raises(VepError) as unavailable:
        FasterWhisperBackend(_complete_model(tmp_path), device="cuda").identity()

    assert unavailable.value.code == "ASR_DEVICE_UNAVAILABLE"
    assert [call["device"] for call in calls] == ["cuda"]


def test_faster_whisper_auto_does_not_hide_unrelated_cuda_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16"}, "cpu": {"int8"}},
        fail_devices={"cuda"},
    )

    with pytest.raises(VepError) as invalid:
        FasterWhisperBackend(_complete_model(tmp_path)).identity()

    assert invalid.value.code == "ASR_DEVICE_UNAVAILABLE"
    assert [call["device"] for call in calls] == ["cuda"]


def test_faster_whisper_explicit_cpu_ignores_available_cuda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    calls = _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=1,
        supported={"cuda": {"float16"}, "cpu": {"int8"}},
    )

    FasterWhisperBackend(_complete_model(tmp_path), device="cpu").identity()

    assert calls[0]["device"] == "cpu"
    assert calls[0]["compute_type"] == "int8"
    assert "device_index" not in calls[0]


def test_faster_whisper_model_hash_is_reused_within_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faster_whisper_module._MODEL_FINGERPRINT_CACHE.clear()
    _install_fake_faster_whisper(
        monkeypatch,
        cuda_count=0,
        supported={"cuda": set(), "cpu": {"int8"}},
    )
    model = _complete_model(tmp_path)
    original = provisioning_module.sha256_file
    hashed: list[Path] = []

    def counted(path: Path) -> str:
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(provisioning_module, "sha256_file", counted)

    FasterWhisperBackend(model).identity()
    FasterWhisperBackend(model).identity()

    assert len(hashed) == 3


def test_artifact_fast_reuse_skips_hash_and_deep_verify_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"small synthetic artifact")
    artifact = ArtifactRef(
        artifact_id="synthetic",
        artifact_type="raw_source",
        content_sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        relative_storage_path="source/raw/synthetic/artifact.bin",
    )
    import video_evidence_pipeline.domain.artifacts as artifacts_module

    original = artifacts_module.sha256_file
    calls = 0

    def counted(candidate: Path) -> str:
        nonlocal calls
        calls += 1
        return original(candidate)

    monkeypatch.setattr(artifacts_module, "sha256_file", counted)

    assert artifact_file_matches(path, artifact)
    assert calls == 0
    metadata = path.stat()
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
    assert not artifact_file_matches(path, artifact)
    assert calls == 0
    assert artifact_file_matches(path, artifact, deep=True)
    assert calls == 1


def test_asr_metric_availability_does_not_create_false_ambiguities(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(workspace, job, audio=True)
    backend = _FakeAsr(
        [
            AsrSegment(
                start_ms=0,
                end_ms=4000,
                raw_text="ordinary ASR output",
                backend_segment_id="ordinary",
                confidence=TranscriptConfidence(
                    kind=TranscriptConfidenceKind.BACKEND_METRIC,
                    avg_logprob=-0.25,
                    no_speech_probability=0.1,
                    word_probability_count=2,
                    word_probability_min=0.7,
                    word_probability_max=0.9,
                    word_probability_mean=0.8,
                ),
                flags=[
                    "no_speech_probability_reported",
                    "word_probabilities_reported",
                ],
            )
        ]
    )
    prepared = prepare_transcript(workspace, job, media, TranscriptionConfig(), backend)
    request = prepare_evidence_request(
        workspace,
        job,
        media,
        _published_manifest_stub(job, prepared),
        prepared.segments,
    ).request

    assert prepared.gaps == ()
    assert prepared.overlaps == ()
    assert request.unresolved_ambiguities == []


def test_true_asr_uncertainty_flag_remains_an_unresolved_ambiguity(tmp_path: Path) -> None:
    job = _job("issue14-uncertain-asr")
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(workspace, job, audio=True)
    backend = _FakeAsr(
        [
            AsrSegment(
                start_ms=0,
                end_ms=4000,
                raw_text="uncertain ASR output",
                backend_segment_id="uncertain",
                confidence=_asr_confidence(),
                flags=["uncertain_speech"],
            )
        ]
    )
    prepared = prepare_transcript(workspace, job, media, TranscriptionConfig(), backend)
    request = prepare_evidence_request(
        workspace,
        job,
        media,
        _published_manifest_stub(job, prepared),
        prepared.segments,
    ).request

    assert len(request.unresolved_ambiguities) == 1
    assert request.unresolved_ambiguities[0].description == (
        "Transcript segment flag: uncertain_speech"
    )
    assert request.unresolved_ambiguities[0].source_refs == [request.transcript_refs[0]]


def _response(
    request,
    *,
    execution_mode: AnalyzerExecutionMode = AnalyzerExecutionMode.EXTERNAL_EXCHANGE,
    external_api_used: bool = False,
    evidence: list[EvidenceRecord] | None = None,
    ambiguities=None,
    request_fingerprint: str | None = None,
) -> EvidenceResponseManifest:
    return EvidenceResponseManifest(
        request_fingerprint=request_fingerprint or request.request_fingerprint,
        analyzer=AnalyzerIdentity(
            provider="local-provider",
            model="local-model",
            version="1",
            executor="external-exchange",
            execution_mode=execution_mode,
            external_api_used=external_api_used,
        ),
        evidence_records=(
            [
                EvidenceRecord(
                    evidence_id="E000001",
                    observation=request.transcript_segments[0].normalized_text,
                    interpretation_level=InterpretationLevel.EXPLICIT_STATEMENT,
                    confidence=EvidenceConfidence.HIGH,
                    source_refs=[request.transcript_refs[0]],
                    asserted_as_video_fact=True,
                )
            ]
            if evidence is None
            else evidence
        ),
        ambiguity_records=list(
            request.unresolved_ambiguities if ambiguities is None else ambiguities
        ),
        created_at=request.created_at + timedelta(seconds=1),
    )


def _write_response(path: Path, response: EvidenceResponseManifest) -> None:
    path.write_bytes(pretty_json_bytes(response))


def _full_caption_request(tmp_path: Path):
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(
        workspace,
        job,
        subtitles=[("ja", SubtitleProvenance.EMBEDDED, True, False, _full_caption("発言"))],
    )
    prepared = prepare_transcript(workspace, job, media, TranscriptionConfig(), None)
    request = prepare_evidence_request(
        workspace, job, media, _published_manifest_stub(job, prepared), prepared.segments
    ).request
    return workspace, job, media, prepared, request


def _published_manifest_stub(job: JobSpec, prepared):
    return TranscriptManifest(
        job_id=job.job_id,
        transcription_fingerprint=prepared.transcription_fingerprint,
        media_bundle_fingerprint=prepared.media_bundle_fingerprint,
        implementation_version="transcription-v1",
        configuration=prepared.configuration,
        backends=list(prepared.backends),
        selected_source_artifacts=list(prepared.selected_source_artifacts),
        selected_subtitle_stream_index=prepared.selected_subtitle_stream_index,
        selected_subtitle_provenance=prepared.selected_subtitle_provenance,
        caption_coverage=prepared.caption_coverage,
        segments_artifact=ArtifactRef(
            artifact_id="transcript-stub",
            artifact_type="transcript_segments",
            content_sha256="e" * 64,
            size_bytes=1,
            relative_storage_path="transcript/segments/stub/transcript.jsonl",
        ),
        segment_count=len(prepared.segments),
        duration_ms=prepared.duration_ms,
        gaps=list(prepared.gaps),
        overlaps=list(prepared.overlaps),
        started_at=prepared.started_at,
        created_at=prepared.started_at,
        warnings=list(prepared.warnings),
    )


def test_evidence_prepare_catalogs_exact_transcript_and_frame_refs_idempotently(
    tmp_path: Path,
) -> None:
    workspace, job, media, prepared, request = _full_caption_request(tmp_path)

    assert request.transcript_segments == list(prepared.segments)
    assert request.transcript_refs[0].content_sha256 == fingerprint(prepared.segments[0])
    assert request.assignment.task_type == job.assignment.task_type
    assert request.assignment.language == job.assignment.language
    assert [item.requirement_id for item in request.assignment.requirements] == [
        "R000001",
        "R000002",
    ]
    assert [item.text for item in request.assignment.requirements] == job.assignment.requirements
    assert request.frame_records == media.frame_index.records
    assert (
        request.frame_refs[0].content_sha256 == media.frame_index.records[0].artifact.content_sha256
    )
    assert request.frame_refs[0].start_ms == media.frame_index.records[0].timestamp_ms
    assert request.approved_artifacts == []
    assert request.approved_artifact_refs == []
    reused = prepare_evidence_request(
        workspace,
        job,
        media,
        _published_manifest_stub(job, prepared),
        prepared.segments,
    )
    assert reused.reused is True
    assert reused.request == request


def test_evidence_import_rejects_fixture_stale_unknown_and_user_opinion(
    tmp_path: Path,
) -> None:
    _workspace_value, _job_value, _media, _prepared, request = _full_caption_request(tmp_path)
    path = tmp_path / "response.json"

    fixture = _response(request, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE)
    _write_response(path, fixture)
    with pytest.raises(VepError) as fixture_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert fixture_error.value.code == "FIXTURE_ANALYZER_FORBIDDEN"
    assert read_evidence_response(path, request, allow_fixture=True) == fixture

    stale = _response(request, request_fingerprint="f" * 64)
    _write_response(path, stale)
    with pytest.raises(VepError) as stale_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert stale_error.value.code == "EVIDENCE_RESPONSE_STALE"

    unknown_ref = SourceRef(
        kind=SourceRefKind.TRANSCRIPT_SEGMENT,
        ref_id="T999999",
        start_ms=0,
        end_ms=4000,
        content_sha256="1" * 64,
    )
    unknown = _response(
        request,
        evidence=[
            EvidenceRecord(
                evidence_id="E000001",
                observation="unknown",
                interpretation_level=InterpretationLevel.EXPLICIT_STATEMENT,
                confidence=EvidenceConfidence.LOW,
                source_refs=[unknown_ref],
            )
        ],
    )
    _write_response(path, unknown)
    with pytest.raises(VepError) as ref_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert ref_error.value.code == "EVIDENCE_SOURCE_REF_INVALID"

    opinion = _response(
        request,
        evidence=[
            EvidenceRecord(
                evidence_id="E000001",
                observation="I liked it",
                interpretation_level=InterpretationLevel.USER_OPINION,
                confidence=EvidenceConfidence.LOW,
                source_refs=[request.transcript_refs[0]],
            )
        ],
    )
    _write_response(path, opinion)
    with pytest.raises(VepError) as opinion_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert opinion_error.value.code == "GENERATED_USER_OPINION_FORBIDDEN"


def test_evidence_interpretation_and_external_api_fixture_contracts(tmp_path: Path) -> None:
    _workspace_value, _job_value, _media, _prepared, request = _full_caption_request(tmp_path)
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id="E000001",
            observation="guess",
            interpretation_level=InterpretationLevel.SPECULATION,
            confidence=EvidenceConfidence.LOW,
            source_refs=[request.transcript_refs[0]],
            asserted_as_video_fact=True,
        )

    approved_artifact = ArtifactRef(
        artifact_id="approved-policy",
        artifact_type="approved_external_source",
        content_sha256="7" * 64,
        size_bytes=42,
        relative_storage_path="evidence/approved/policy.json",
    )
    approved_ref = SourceRef(
        kind=SourceRefKind.APPROVED_ARTIFACT,
        ref_id=approved_artifact.artifact_id,
        content_sha256=approved_artifact.content_sha256,
    )
    external = EvidenceRecord(
        evidence_id="E000001",
        observation="approved external fact",
        interpretation_level=InterpretationLevel.EXTERNAL_FACT,
        confidence=EvidenceConfidence.LOW,
        source_refs=[approved_ref],
    )
    assert external.asserted_as_video_fact is False
    unknown_external = _response(request, evidence=[external])
    external_path = tmp_path / "unknown-external.json"
    _write_response(external_path, unknown_external)
    with pytest.raises(VepError) as external_error:
        read_evidence_response(external_path, request, allow_fixture=False)
    assert external_error.value.code == "EVIDENCE_SOURCE_REF_INVALID"
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id="E000001",
            observation="external",
            interpretation_level=InterpretationLevel.EXTERNAL_FACT,
            confidence=EvidenceConfidence.LOW,
            source_refs=[request.transcript_refs[0]],
            asserted_as_video_fact=True,
        )

    invalid_fixture = _response(
        request,
        execution_mode=AnalyzerExecutionMode.TEST_FIXTURE,
        external_api_used=True,
    )
    path = tmp_path / "fixture-external.json"
    _write_response(path, invalid_fixture)
    with pytest.raises(VepError) as fixture_error:
        read_evidence_response(path, request, allow_fixture=True)
    assert fixture_error.value.code == "FIXTURE_ANALYZER_INVALID"


def test_analyzer_cannot_self_approve_evidence_or_ambiguity(tmp_path: Path) -> None:
    _workspace_value, _job_value, _media, _prepared, request = _full_caption_request(tmp_path)
    accepted = (
        _response(request)
        .evidence_records[0]
        .model_copy(update={"review_status": ReviewStatus.ACCEPTED})
    )
    path = tmp_path / "self-reviewed.json"
    _write_response(path, _response(request, evidence=[accepted]))
    with pytest.raises(VepError) as evidence_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert evidence_error.value.code == "ANALYZER_REVIEW_STATUS_FORBIDDEN"

    self_reviewed = AmbiguityRecord(
        ambiguity_id="A000001",
        description="analyzer cannot approve this",
        start_ms=request.transcript_refs[0].start_ms,
        end_ms=request.transcript_refs[0].end_ms,
        source_refs=[request.transcript_refs[0]],
        review_status=ReviewStatus.ACCEPTED,
    )
    _write_response(
        path,
        _response(
            request,
            ambiguities=[self_reviewed],
        ),
    )
    with pytest.raises(VepError) as ambiguity_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert ambiguity_error.value.code == "ANALYZER_REVIEW_STATUS_FORBIDDEN"


def test_unresolved_ambiguities_cannot_be_dropped_or_resolved(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(workspace, job, audio=True)
    backend = _FakeAsr(
        [
            AsrSegment(
                start_ms=1000,
                end_ms=3000,
                raw_text="partial",
                backend_segment_id="partial",
                confidence=_asr_confidence(),
            )
        ]
    )
    prepared = prepare_transcript(workspace, job, media, TranscriptionConfig(), backend)
    request = prepare_evidence_request(
        workspace, job, media, _published_manifest_stub(job, prepared), prepared.segments
    ).request
    assert request.unresolved_ambiguities
    path = tmp_path / "ambiguity-response.json"

    omitted = _response(request, ambiguities=[])
    _write_response(path, omitted)
    with pytest.raises(VepError) as omitted_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert omitted_error.value.code == "UNRESOLVED_AMBIGUITY_LOST"

    resolved = request.unresolved_ambiguities[0].model_copy(
        update={
            "status": AmbiguityStatus.RESOLVED,
            "resolution": "not independently established",
            "review_status": ReviewStatus.NEEDS_REVIEW,
        }
    )
    altered = _response(
        request,
        ambiguities=[resolved, *request.unresolved_ambiguities[1:]],
    )
    _write_response(path, altered)
    with pytest.raises(VepError) as altered_error:
        read_evidence_response(path, request, allow_fixture=False)
    assert altered_error.value.code == "UNRESOLVED_AMBIGUITY_LOST"


def test_transcript_and_evidence_publish_recover_and_detect_tampering(tmp_path: Path) -> None:
    job = _job()
    workspace = _workspace(tmp_path, job)
    media = _media_manifest(
        workspace,
        job,
        subtitles=[("ja", SubtitleProvenance.EMBEDDED, True, False, _full_caption("発言"))],
    )
    prepared = prepare_transcript(workspace, job, media, TranscriptionConfig(), None)
    transcript_attempt = create_transcript_attempt(workspace)
    transcript = publish_transcript(workspace, job, media, prepared, transcript_attempt)
    assert transcript.reused is False
    assert load_transcription_result(workspace, job, media).segments == prepared.segments

    workspace.transcript_manifest_json.unlink()
    recovered_transcript = recover_transcription_result(workspace, job, media)
    assert recovered_transcript is not None
    assert recovered_transcript.reused is True
    assert workspace.transcript_manifest_json.exists()

    request = prepare_evidence_request(
        workspace,
        job,
        media,
        recovered_transcript.transcript_manifest,
        recovered_transcript.segments,
    ).request
    response = _response(request)
    response_path = tmp_path / "valid-response.json"
    _write_response(response_path, response)
    validated = read_evidence_response(response_path, request, allow_fixture=False)
    evidence_attempt = create_evidence_attempt(workspace)
    evidence = publish_evidence(
        workspace,
        job,
        media,
        recovered_transcript.transcript_manifest,
        recovered_transcript.segments,
        request,
        validated,
        evidence_attempt,
    )
    assert evidence.reused is False
    assert evidence.stage_manifest.input_artifacts == [
        recovered_transcript.transcript_manifest.segments_artifact,
        media.frame_index_artifact,
    ]
    assert evidence.stage_manifest.output_artifacts == [
        evidence.evidence_manifest.response_artifact,
        evidence.evidence_manifest.evidence_artifact,
        evidence.evidence_manifest.ambiguity_artifact,
    ]
    assert evidence.stage_manifest.producer.name == "video-evidence-pipeline-evidence-import"
    assert (
        type(evidence.stage_manifest).model_validate_json(
            evidence.stage_manifest.model_dump_json(), strict=True
        )
        == evidence.stage_manifest
    )
    assert load_evidence_result(
        workspace,
        job,
        media,
        recovered_transcript.transcript_manifest,
        recovered_transcript.segments,
    ).evidence_records == tuple(response.evidence_records)

    workspace.evidence_manifest_json.unlink()
    recovered_evidence = recover_evidence_result(
        workspace,
        job,
        media,
        recovered_transcript.transcript_manifest,
        recovered_transcript.segments,
    )
    assert recovered_evidence is not None
    assert recovered_evidence.reused is True
    assert workspace.evidence_manifest_json.exists()

    transcript_path = workspace.safe_path(
        recovered_transcript.transcript_manifest.segments_artifact.relative_storage_path
    )
    transcript_path.write_bytes(transcript_path.read_bytes() + b"tamper")
    with pytest.raises(VepError) as tampered:
        load_transcription_result(workspace, job, media)
    assert tampered.value.code == "TRANSCRIPT_BUNDLE_TAMPERED"


def test_zero_evidence_is_canonical_rebuildable_and_does_not_weaken_artifacts(
    tmp_path: Path,
) -> None:
    empty_sha256 = sha256_bytes(b"")
    empty_ref = EvidenceLedgerArtifactRef(
        artifact_id="evidence-records-empty",
        content_sha256=empty_sha256,
        size_bytes=0,
        relative_storage_path=f"evidence/records/{empty_sha256}/evidence.jsonl",
    )
    assert (
        EvidenceLedgerArtifactRef.model_validate_json(empty_ref.model_dump_json(), strict=True)
        == empty_ref
    )
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id="raw-empty",
            artifact_type="raw_source",
            content_sha256=empty_sha256,
            size_bytes=0,
            relative_storage_path="source/raw/empty.bin",
        )
    with pytest.raises(ValidationError):
        EvidenceLedgerArtifactRef(
            artifact_id="evidence-records-empty",
            content_sha256="0" * 64,
            size_bytes=0,
            relative_storage_path="evidence/records/invalid/evidence.jsonl",
        )
    assert ArtifactRef.model_json_schema()["properties"]["size_bytes"]["minimum"] == 1
    assert EvidenceLedgerArtifactRef.model_json_schema()["properties"]["size_bytes"]["minimum"] == 0
    assert (
        "minItems"
        not in EvidenceResponseManifest.model_json_schema()["properties"]["evidence_records"]
    )
    assert EvidenceManifest.model_json_schema()["properties"]["evidence_count"]["minimum"] == 0

    workspace, _transcription = _application_transcribed_job(tmp_path, "issue14-empty-evidence")
    request = prepare_evidence_job(workspace.root).request
    response_path = tmp_path / "empty-evidence-response.json"
    _write_response(
        response_path,
        _response(
            request,
            execution_mode=AnalyzerExecutionMode.TEST_FIXTURE,
            evidence=[],
        ),
    )
    imported = _import_evidence_fixture(workspace.root, response_path)

    assert imported.evidence_records == ()
    assert imported.evidence_manifest.evidence_count == 0
    assert imported.evidence_manifest.evidence_artifact == empty_ref.model_copy(
        update={
            "artifact_id": f"evidence_records-{empty_sha256[:16]}",
        }
    )
    evidence_path = workspace.safe_path(
        imported.evidence_manifest.evidence_artifact.relative_storage_path
    )
    assert evidence_path.read_bytes() == b""
    assert (
        type(imported.stage_manifest).model_validate_json(
            imported.stage_manifest.model_dump_json(), strict=True
        )
        == imported.stage_manifest
    )
    bad_stage = imported.stage_manifest.model_dump(mode="json")
    bad_stage["output_artifacts"][1] = {
        "artifact_id": "raw-empty",
        "artifact_type": "raw_source",
        "content_sha256": empty_sha256,
        "size_bytes": 0,
        "relative_storage_path": "source/raw/empty.bin",
    }
    with pytest.raises(ValidationError):
        type(imported.stage_manifest).model_validate(bad_stage, strict=True)
    reused = _import_evidence_fixture(workspace.root, response_path)
    assert reused.reused is True
    assert reused.evidence_records == ()
    assert validate_job(workspace.root)["state"] == JobState.EVIDENCE_READY.value
    built = build_search_index_job(workspace.root)
    assert built.metadata.evidence_count == 0
    result = search_job(workspace.root, "application", 10)
    assert result.hits
    assert all(item.kind.value == "transcript" for item in result.hits)


@pytest.mark.parametrize("field", ["name", "version", "runtime", "model"])
def test_transcription_backend_identity_rejects_blank_metadata(field: str) -> None:
    payload = {
        "source": TranscriptSource.ASR,
        "name": "faster-whisper",
        "version": "1.2.3",
        "runtime": "ctranslate2",
        "model": "local-model",
    }
    payload[field] = " \t "
    with pytest.raises(ValidationError):
        TranscriptionBackendIdentity.model_validate(payload, strict=True)


def test_application_and_cli_complete_both_transitions_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, transcription = _application_transcribed_job(tmp_path, "issue14-application")
    config = TranscriptionConfig(preferred_languages=["en"], minimum_caption_coverage=0.8)
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in transitions] == [
        JobState.CREATED,
        JobState.AUTHORIZED,
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
    ]

    reused_transcript = transcribe_job(workspace.root, config)
    assert reused_transcript.reused is True
    assert read_jsonl(workspace.transitions_jsonl, TransitionRecord) == transitions
    with pytest.raises(VepError) as config_error:
        transcribe_job(
            workspace.root,
            TranscriptionConfig(preferred_languages=["ja"], minimum_caption_coverage=0.8),
        )
    assert config_error.value.code == "TRANSCRIPT_MANIFEST_MISMATCH"

    request = prepare_evidence_job(workspace.root).request
    assert request.transcript_segments == list(transcription.segments)
    external_response = _response(request, external_api_used=True)
    external_path = tmp_path / "external-response.json"
    _write_response(external_path, external_response)
    with pytest.raises(VepError) as authorization_error:
        import_evidence_job(workspace.root, external_path)
    assert authorization_error.value.code == "EXTERNAL_API_NOT_AUTHORIZED"

    fixture_response = _response(request, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE)
    response_path = tmp_path / "fixture-response.json"
    _write_response(response_path, fixture_response)
    runner = CliRunner()
    production = runner.invoke(
        app,
        [
            "evidence",
            "import",
            str(workspace.root),
            "--response",
            str(response_path),
            "--json",
        ],
    )
    assert production.exit_code != 0
    assert json.loads(production.stdout)["error"]["code"] == "FIXTURE_ANALYZER_FORBIDDEN"

    original_stage = application_module.stage_evidence
    transition_lock_released = threading.Event()

    def observing_stage(*args, **kwargs):
        failures: list[BaseException] = []

        def acquire_transition_lock() -> None:
            try:
                with exclusive_append_lock(workspace.transitions_jsonl):
                    transition_lock_released.set()
            except BaseException as error:  # pragma: no cover - surfaced below
                failures.append(error)

        thread = threading.Thread(target=acquire_transition_lock, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not failures
        assert transition_lock_released.is_set()
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(application_module, "stage_evidence", observing_stage)
    imported = _import_evidence_fixture(workspace.root, response_path)
    assert imported.reused is False
    assert transition_lock_released.is_set()
    committed_transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert [item.to_state for item in committed_transitions] == [
        JobState.CREATED,
        JobState.AUTHORIZED,
        JobState.ACQUIRED,
        JobState.NORMALIZED,
        JobState.TRANSCRIBED,
        JobState.EVIDENCE_READY,
    ]
    assert committed_transitions[-1].trigger == "evidence.import"

    reused_evidence = _import_evidence_fixture(workspace.root, response_path)
    assert reused_evidence.reused is True
    assert read_jsonl(workspace.transitions_jsonl, TransitionRecord) == committed_transitions
    changed_response = _response(
        request,
        execution_mode=AnalyzerExecutionMode.TEST_FIXTURE,
        evidence=[
            fixture_response.evidence_records[0].model_copy(
                update={"observation": "different validated observation"}
            )
        ],
    )
    changed_path = tmp_path / "changed-response.json"
    _write_response(changed_path, changed_response)
    with pytest.raises(VepError) as response_error:
        _import_evidence_fixture(workspace.root, changed_path)
    assert response_error.value.code == "EVIDENCE_FINGERPRINT_MISMATCH"

    assert validate_job(workspace.root)["state"] == JobState.EVIDENCE_READY.value
    status, exit_code = job_status(workspace.root)
    assert exit_code == 0
    assert status["stored_state"] == JobState.EVIDENCE_READY.value
    assert status["transcription_integrity"]["status"] == "valid"
    assert status["evidence_integrity"] == {
        "status": "valid",
        "evidence_bundle_fingerprint": imported.evidence_manifest.evidence_bundle_fingerprint,
        "evidence_count": 1,
        "ambiguity_count": len(imported.ambiguity_records),
        "index_rebuildable": True,
    }

    prepare_cli = runner.invoke(app, ["evidence", "prepare", str(workspace.root), "--json"])
    assert prepare_cli.exit_code == 0
    prepare_payload = json.loads(prepare_cli.stdout)
    assert prepare_payload["reused"] is True
    assert "state" not in prepare_payload

    built = build_search_index_job(workspace.root)
    assert built.metadata.tokenizer == "trigram"
    direct_search = search_job(workspace.root, "app", 10)
    assert direct_search.hits
    index_cli = runner.invoke(app, ["index", "build", str(workspace.root), "--json"])
    assert index_cli.exit_code == 0
    assert json.loads(index_cli.stdout)["reused"] is True
    search_cli = runner.invoke(
        app, ["search", str(workspace.root), "app", "--limit", "10", "--json"]
    )
    assert search_cli.exit_code == 0
    assert json.loads(search_cli.stdout)["hits"]


def test_evidence_snapshot_tamper_publishes_no_canonical_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _transcription = _application_transcribed_job(tmp_path, "issue14-stale")
    request = prepare_evidence_job(workspace.root).request
    response_path = tmp_path / "stale-response.json"
    _write_response(
        response_path,
        _response(request, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE),
    )
    original_stage = application_module.stage_evidence

    def tampering_stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        media_manifest = args[2]
        frame_path = workspace.safe_path(
            media_manifest.frame_index.records[0].artifact.relative_storage_path
        )
        payload = bytearray(frame_path.read_bytes())
        payload[0] ^= 0x01
        frame_path.write_bytes(payload)
        return staged

    monkeypatch.setattr(application_module, "stage_evidence", tampering_stage)
    with pytest.raises(VepError):
        _import_evidence_fixture(workspace.root, response_path)

    assert not workspace.evidence_candidate_manifest_json.exists()
    assert not workspace.evidence_stage_manifest_json.exists()
    assert not workspace.evidence_manifest_json.exists()
    assert not workspace.safe_path("evidence/responses").exists()
    assert not workspace.safe_path("evidence/records").exists()
    assert not workspace.safe_path("evidence/ambiguities").exists()
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert transitions[-1].to_state is JobState.TRANSCRIBED


def test_evidence_tamper_after_outside_revalidation_is_rejected_under_final_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _transcription = _application_transcribed_job(tmp_path, "issue14-final-lock-stale")
    request = prepare_evidence_job(workspace.root).request
    response_path = tmp_path / "final-lock-stale-response.json"
    _write_response(
        response_path,
        _response(request, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE),
    )
    original_load_request = application_module.load_evidence_request
    load_count = 0

    def tamper_after_last_outside_request(*args, **kwargs):
        nonlocal load_count
        loaded = original_load_request(*args, **kwargs)
        load_count += 1
        if load_count == 2:
            media_manifest = args[2]
            frame_path = workspace.safe_path(
                media_manifest.frame_index.records[0].artifact.relative_storage_path
            )
            payload = bytearray(frame_path.read_bytes())
            payload[0] ^= 0x01
            frame_path.write_bytes(payload)
        return loaded

    monkeypatch.setattr(
        application_module,
        "load_evidence_request",
        tamper_after_last_outside_request,
    )
    with pytest.raises(VepError):
        _import_evidence_fixture(workspace.root, response_path)

    assert load_count == 2
    assert not workspace.evidence_candidate_manifest_json.exists()
    assert not workspace.evidence_stage_manifest_json.exists()
    assert not workspace.evidence_manifest_json.exists()
    assert not workspace.safe_path("evidence/responses").exists()
    assert not workspace.safe_path("evidence/records").exists()
    assert not workspace.safe_path("evidence/ambiguities").exists()
    transitions = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert transitions[-1].to_state is JobState.TRANSCRIBED
