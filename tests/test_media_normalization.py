from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import video_evidence_pipeline.application as application_module
import video_evidence_pipeline.media.frames as frames_module
import video_evidence_pipeline.media.normalization as normalization_module
import video_evidence_pipeline.media.subtitles as subtitles_module
from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    initialize_job,
    job_status,
    normalize_job,
    validate_job,
)
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.media import (
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
    SubtitleCue,
    SubtitleKind,
    SubtitleProvenance,
    SubtitleStatus,
    ToolIdentity,
)
from video_evidence_pipeline.domain.serialization import canonical_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError, configuration_error, integrity_error
from video_evidence_pipeline.media.budget import StagingBudget
from video_evidence_pipeline.media.commands import (
    SAFE_DEMUXER_WHITELIST,
    ffmpeg_local_input,
    ffprobe_local_input,
)
from video_evidence_pipeline.media.frames import (
    StagedFrame,
    build_candidates,
    extract_frames,
    merge_near_duplicate,
    scene_change_candidates,
)
from video_evidence_pipeline.media.normalization import (
    _run_analysis_proxy,
    _run_speech_audio,
    cleanup_stale_attempts,
)
from video_evidence_pipeline.media.staging import prepare_private_output
from video_evidence_pipeline.media.subtitles import (
    extract_subtitles,
    parse_webvtt,
    verify_cue_jsonl,
)
from video_evidence_pipeline.storage.atomic import atomic_write_json
from video_evidence_pipeline.storage.jsonl import read_jsonl


def test_webvtt_model_validation_maps_to_safe_media_error() -> None:
    payload = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n" + "x" * 65_537 + "\n").encode()

    with pytest.raises(VepError) as captured:
        parse_webvtt(payload, stream_index=0, duration_ms=1000)

    assert captured.value.code == "SUBTITLE_VTT_INVALID"


def test_normalization_config_keeps_finite_subtitle_and_decoder_slot_caps() -> None:
    with pytest.raises(ValueError):
        NormalizationConfig(maximum_subtitle_bytes=64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        NormalizationConfig(maximum_decoded_video_pixel_slots_per_frame=34_070_529)
    with pytest.raises(ValueError):
        NormalizationConfig(maximum_decoded_audio_sample_slots_per_frame=1_048_577)


def _required_media_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    if os.environ.get("VEP_REQUIRE_FFMPEG_TESTS") == "1":
        pytest.fail("VEP_REQUIRE_FFMPEG_TESTS=1 but system ffmpeg/ffprobe are unavailable")
    pytest.skip("system ffmpeg/ffprobe are unavailable; actual synthetic integration was not run")


def _synthetic_source(
    root: Path,
    *,
    audio: bool,
    subtitle: bool,
    frame_rate: int = 10,
    subtitle_payload: str | None = None,
) -> Path:
    ffmpeg, _ffprobe = _required_media_tools()
    target = root / ("synthetic.mkv" if subtitle else "synthetic.mp4")
    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate={frame_rate}:duration=3",
    ]
    if audio:
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=48000:duration=3",
            ]
        )
    if subtitle:
        subtitle_path = root / "synthetic.srt"
        subtitle_path.write_text(
            subtitle_payload or "1\n00:00:00,250 --> 00:00:01,500\nsynthetic subtitle text\n",
            encoding="utf-8",
        )
        argv.extend(["-i", str(subtitle_path)])
    argv.extend(["-map", "0:v:0"])
    next_input = 1
    if audio:
        argv.extend(["-map", f"{next_input}:a:0"])
        next_input += 1
    if subtitle:
        argv.extend(["-map", f"{next_input}:s:0"])
    argv.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    if audio:
        argv.extend(["-c:a", "aac"])
    if subtitle:
        argv.extend(["-c:s", "subrip"])
    argv.extend(["-t", "3", "-y", str(target)])
    subprocess.run(
        argv,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return target


def _acquired_job(root: Path, source: Path, job_id: str):
    workspace, _job = initialize_job(
        root,
        job_id=job_id,
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    result = acquire_job(workspace.root)
    return workspace, result.source_manifest.artifact_record.artifact


def _synthetic_identities() -> tuple[ToolIdentity, ToolIdentity]:
    return (
        ToolIdentity(
            name="ffmpeg",
            version="ffmpeg synthetic",
            executable_sha256="a" * 64,
            capabilities=[],
        ),
        ToolIdentity(
            name="ffprobe",
            version="ffprobe synthetic",
            executable_sha256="b" * 64,
            capabilities=[],
        ),
    )


def test_normalization_publication_hashes_staged_bytes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _job = initialize_job(
        tmp_path,
        job_id="publication-hash-count",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(tmp_path / "source.mp4"),
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"synthetic immutable media")
    original = normalization_module.sha256_file
    hashed: list[Path] = []

    def counted(path: Path) -> str:
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(normalization_module, "sha256_file", counted)

    artifact = normalization_module._artifact_for(
        workspace,
        staged,
        artifact_prefix="analysis",
        artifact_type="analysis_proxy",
        relative_directory="analysis",
        filename="analysis.mp4",
    )

    assert hashed == [staged]
    target = workspace.safe_path(artifact.relative_storage_path)
    assert target.read_bytes() == staged.read_bytes()


def test_normalization_publication_defers_existing_target_integrity_to_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _job = initialize_job(
        tmp_path,
        job_id="publication-existing-target",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(tmp_path / "source.mp4"),
        task_type="report",
        language="ja",
        output_format="markdown",
    )
    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"correct")
    content_sha256 = sha256_file(staged)
    target = workspace.safe_path(f"normalized/analysis/{content_sha256}/analysis.mp4")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"tamper!")
    original = normalization_module.sha256_file
    hashed: list[Path] = []

    def counted(path: Path) -> str:
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(normalization_module, "sha256_file", counted)

    artifact = normalization_module._artifact_for(
        workspace,
        staged,
        artifact_prefix="analysis",
        artifact_type="analysis_proxy",
        relative_directory="analysis",
        filename="analysis.mp4",
    )

    assert hashed == [staged]
    with pytest.raises(VepError) as captured:
        normalization_module._verify_artifact_path(target, artifact, deep=True)
    assert captured.value.code == "NORMALIZED_BUNDLE_TAMPERED"
    assert hashed == [staged, target]


def _probe_with_subtitle(codec_name: str | None = None) -> MediaProbe:
    streams = [
        MediaStream(
            index=0,
            stream_type=MediaStreamType.VIDEO,
            codec_name="h264",
            time_base=MediaRational(numerator=1, denominator=1000),
            frame_rate=MediaRational(numerator=2, denominator=1),
            width=320,
            height=180,
        )
    ]
    if codec_name is not None:
        streams.append(
            MediaStream(
                index=1,
                stream_type=MediaStreamType.SUBTITLE,
                codec_name=codec_name,
                time_base=MediaRational(numerator=1, denominator=1000),
                subtitle_kind=SubtitleKind.TEXT,
                subtitle_provenance=SubtitleProvenance.EMBEDDED,
                is_default=False,
                is_forced=False,
            )
        )
    return MediaProbe(format_name="mov,mp4", duration_ms=1000, streams=streams)


def _frame_artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="frame-test",
        artifact_type="extracted_frame",
        content_sha256="a" * 64,
        size_bytes=1,
        relative_storage_path="normalized/frames/a/frame.png",
    )


def test_fixed_local_input_policy_excludes_external_reference_demuxers(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.mp4"
    ffmpeg_arguments = ffmpeg_local_input(source, 7, 1234, 5678)
    ffprobe_arguments = ffprobe_local_input(source, 7, 1234, 5678)
    for arguments in (ffmpeg_arguments, ffprobe_arguments):
        assert arguments[arguments.index("-protocol_whitelist") + 1] == "file"
        assert arguments[arguments.index("-format_whitelist") + 1] == SAFE_DEMUXER_WHITELIST
        assert all(
            name not in SAFE_DEMUXER_WHITELIST.split(",")
            for name in ("hls", "dash", "concat", "image2", "sdp")
        )
        maximum_streams = arguments.index("-max_streams")
        assert arguments[maximum_streams + 1] == "7"
        assert maximum_streams < arguments.index(str(source.absolute()))
        maximum_pixels = arguments.index("-max_pixels")
        assert arguments[maximum_pixels + 1] == "1234"
        maximum_samples = arguments.index("-max_samples")
        assert arguments[maximum_samples + 1] == "5678"
        assert maximum_pixels < arguments.index(str(source.absolute()))
        assert maximum_samples < arguments.index(str(source.absolute()))
    assert ffmpeg_arguments.index("-max_streams") < ffmpeg_arguments.index("-i")


def test_analysis_speech_and_scene_decode_commands_place_slot_limits_before_input(
    tmp_path: Path,
) -> None:
    config = NormalizationConfig(
        maximum_decoded_video_pixel_slots_per_frame=12_345,
        maximum_decoded_audio_sample_slots_per_frame=6_789,
    )
    observed: list[list[str]] = []

    class RecordingRunner:
        def run_checked(self, _tool: str, arguments: list[str], **_kwargs: object):
            observed.append(arguments)
            return SimpleNamespace(stderr=b"")

    runner = RecordingRunner()
    source = tmp_path / "source.mp4"
    _run_analysis_proxy(runner, source, tmp_path / "analysis.mp4", config)  # type: ignore[arg-type]
    _run_speech_audio(runner, source, tmp_path / "speech.wav", config)  # type: ignore[arg-type]
    scene_change_candidates(runner, source, _probe_with_subtitle(), config)  # type: ignore[arg-type]

    assert len(observed) == 3
    for arguments in observed:
        input_index = arguments.index("-i")
        max_pixels = arguments.index("-max_pixels")
        max_samples = arguments.index("-max_samples")
        assert arguments[max_pixels + 1] == "12345"
        assert arguments[max_samples + 1] == "6789"
        assert max_pixels < input_index
        assert max_samples < input_index


def test_periodic_and_scene_candidates_merge_reasons_in_integer_milliseconds() -> None:
    config = NormalizationConfig(
        periodic_frame_interval_seconds=1,
        timestamp_merge_tolerance_seconds=0.005,
    )
    candidates = build_candidates(3500, [1002, 2500], config)
    merged = next(item for item in candidates if item.timestamp_ms == 1000)
    assert merged.reasons == (FrameReason.PERIODIC, FrameReason.SCENE_CHANGE)
    assert merged.source_timestamps_ms == (1000, 1002)
    assert all(isinstance(item.timestamp_ms, int) for item in candidates)


def test_periodic_candidates_fail_before_materializing_an_oversized_range() -> None:
    config = NormalizationConfig(
        periodic_frame_interval_seconds=0.001,
        maximum_frame_candidates=2,
    )
    with pytest.raises(VepError) as captured:
        build_candidates(10_000_000, [], config)
    assert captured.value.code == "FRAME_CANDIDATE_LIMIT_EXCEEDED"


def test_near_duplicate_phash_merges_candidate_provenance(tmp_path: Path) -> None:
    retained = [
        StagedFrame(
            path=tmp_path / "first.png",
            timestamp_ms=1000,
            perceptual_hash="0000000000000000",
            reasons=(FrameReason.PERIODIC,),
            candidate_timestamps_ms=(1000,),
        )
    ]
    duplicate = StagedFrame(
        path=tmp_path / "duplicate.png",
        timestamp_ms=1002,
        perceptual_hash="0000000000000001",
        reasons=(FrameReason.SCENE_CHANGE,),
        candidate_timestamps_ms=(1002,),
    )
    assert merge_near_duplicate(retained, duplicate, maximum_distance=1) is True
    assert len(retained) == 1
    assert retained[0].reasons == (FrameReason.PERIODIC, FrameReason.SCENE_CHANGE)
    assert retained[0].candidate_timestamps_ms == (1000, 1002)


def test_frame_index_keeps_requested_candidates_when_observed_pts_differs() -> None:
    frame_index = FrameIndex(
        duration_ms=1000,
        records=[
            FrameRecord(
                frame_id="F000001",
                timestamp_ms=500,
                artifact=_frame_artifact(),
                perceptual_hash="0" * 16,
                reasons=[FrameReason.PERIODIC],
                candidate_timestamps_ms=[300],
            )
        ],
        candidate_count=1,
        duplicate_count=0,
    )

    assert frame_index.records[0].timestamp_ms == 500
    assert frame_index.records[0].candidate_timestamps_ms == [300]


def test_observed_frame_pts_are_normalized_from_container_start_time() -> None:
    payload = b"[Parsed_showinfo] n: 0 pts: 67584 pts_time:6.600 duration: 1\n"

    assert (
        frames_module._parse_extracted_frame_timestamp(
            payload,
            duration_ms=3000,
            start_time_ms=5000,
        )
        == 1600
    )


def test_ass_subtitle_is_recorded_unsupported_without_source_text_claim(tmp_path: Path) -> None:
    class NoToolRunner:
        def run_checked(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("ASS must not be normalized through the bounded text path")

    config = NormalizationConfig()
    staged = extract_subtitles(
        NoToolRunner(),  # type: ignore[arg-type]
        tmp_path / "source.mp4",
        _probe_with_subtitle("ass"),
        config,
        tmp_path,
        StagingBudget(config.maximum_output_bytes),
    )

    assert staged[0].status is SubtitleStatus.UNSUPPORTED
    assert staged[0].warning_code == "SUBTITLE_NORMALIZATION_UNSUPPORTED"
    normalized = NormalizedSubtitle(
        stream_index=1,
        codec_name="ass",
        kind=SubtitleKind.TEXT,
        provenance=SubtitleProvenance.EMBEDDED,
        is_default=False,
        is_forced=False,
        status=SubtitleStatus.UNSUPPORTED,
        warning_code="SUBTITLE_NORMALIZATION_UNSUPPORTED",
    )
    assert normalized.raw_text_preserved is False
    normalized = NormalizedSubtitle(
        stream_index=1,
        codec_name="subrip",
        kind=SubtitleKind.TEXT,
        provenance=SubtitleProvenance.EMBEDDED,
        is_default=False,
        is_forced=False,
        status=SubtitleStatus.NORMALIZED,
        webvtt_artifact=_frame_artifact(),
        cues_artifact=_frame_artifact(),
    )
    assert normalized.raw_text_preserved is False
    with pytest.raises(ValueError):
        NormalizedSubtitle(
            stream_index=1,
            codec_name="subrip",
            kind=SubtitleKind.TEXT,
            provenance=SubtitleProvenance.EMBEDDED,
            is_default=False,
            is_forced=False,
            status=SubtitleStatus.NORMALIZED,
            webvtt_artifact=_frame_artifact(),
            cues_artifact=_frame_artifact(),
            raw_text_preserved=True,
        )


def test_oversized_subtitle_is_rejected_before_reading_into_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NormalizationConfig(maximum_subtitle_bytes=1024 * 1024)

    class OversizedSubtitleRunner:
        def run_checked(self, _tool: str, arguments: list[str], **_kwargs: object) -> object:
            input_index = arguments.index("-i")
            assert arguments[arguments.index("-max_pixels") + 1] == str(
                config.maximum_decoded_video_pixel_slots_per_frame
            )
            assert arguments[arguments.index("-max_samples") + 1] == str(
                config.maximum_decoded_audio_sample_slots_per_frame
            )
            assert arguments.index("-max_pixels") < input_index
            assert arguments.index("-max_samples") < input_index
            Path(arguments[-1]).write_bytes(b"WEBVTT\n\n" + b"x" * (1024 * 1024))
            return object()

    def fail_read(self: Path) -> bytes:
        if self.name.startswith("subtitle-"):
            pytest.fail("oversized subtitle was read before its stat limit was checked")
        return b""

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(VepError) as captured:
        extract_subtitles(
            OversizedSubtitleRunner(),  # type: ignore[arg-type]
            tmp_path / "source.mp4",
            _probe_with_subtitle("subrip"),
            config,
            tmp_path,
            StagingBudget(config.maximum_output_bytes),
        )

    assert captured.value.code == "NORMALIZED_OUTPUT_TOO_LARGE"


def test_oversized_reused_cue_jsonl_is_rejected_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cue_path = tmp_path / "oversized.jsonl"
    maximum_bytes = 1024 * 1024
    cue_path.write_bytes(b"x" * (maximum_bytes + 1))

    def fail_read(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("oversized reused cue JSONL was read before its stat limit was checked")

    monkeypatch.setattr(subtitles_module.os, "read", fail_read)
    with pytest.raises(VepError) as captured:
        verify_cue_jsonl(cue_path, maximum_bytes=maximum_bytes)

    assert captured.value.code == "NORMALIZED_OUTPUT_TOO_LARGE"


def test_frame_generation_stops_before_next_output_when_cumulative_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NormalizationConfig(
        maximum_duration_ms=1000,
        maximum_output_bytes=1536 * 1024,
        maximum_output_file_bytes=1024 * 1024,
        maximum_frame_output_bytes=1024 * 1024,
        maximum_subtitle_bytes=1024 * 1024,
        periodic_frame_interval_seconds=0.5,
    )
    call_count = 0

    class FrameRunner:
        def run_checked(self, _tool: str, arguments: list[str], **_kwargs: object):
            nonlocal call_count
            call_count += 1
            input_index = arguments.index("-i")
            assert arguments[arguments.index("-max_pixels") + 1] == str(
                config.maximum_decoded_video_pixel_slots_per_frame
            )
            assert arguments[arguments.index("-max_samples") + 1] == str(
                config.maximum_decoded_audio_sample_slots_per_frame
            )
            assert arguments.index("-max_pixels") < input_index
            assert arguments.index("-max_samples") < input_index
            Path(arguments[-1]).write_bytes(b"x" * (1024 * 1024))
            return SimpleNamespace(stderr=b"pts_time:0")

    monkeypatch.setattr(frames_module, "scene_change_candidates", lambda *_args: [])
    monkeypatch.setattr(frames_module, "perceptual_hash_png", lambda *_args, **_kwargs: "0" * 16)
    with pytest.raises(VepError) as captured:
        extract_frames(
            FrameRunner(),  # type: ignore[arg-type]
            tmp_path / "source.mp4",
            _probe_with_subtitle(),
            config,
            tmp_path,
            StagingBudget(config.maximum_output_bytes),
        )

    assert captured.value.code == "NORMALIZED_OUTPUT_TOO_LARGE"
    assert call_count == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not a Windows guarantee")
def test_media_staging_output_is_precreated_private(tmp_path: Path) -> None:
    output = tmp_path / "private" / "analysis.mp4"
    prepare_private_output(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_malformed_media_fails_without_manifest_transition_or_staging(tmp_path: Path) -> None:
    source = tmp_path / "malformed.bin"
    source.write_bytes(b"not media")
    workspace, _raw_artifact = _acquired_job(tmp_path, source, "malformed-media")

    class MalformedRunner:
        def discover_identities(self, **_: object):
            return _synthetic_identities()

        def pin_identities(self, *_args: object) -> None:
            return None

        def revalidate_pinned_identities(self, *_args: object) -> None:
            return None

        def run_checked(self, *_args: object, **_kwargs: object):
            raise integrity_error("FFPROBE_OUTPUT_INVALID", "ffprobe output is malformed")

    with pytest.raises(VepError) as captured:
        normalize_job(workspace.root, runner_override=MalformedRunner())  # type: ignore[arg-type]
    assert captured.value.code == "FFPROBE_OUTPUT_INVALID"
    records = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert records[-1].to_state is JobState.ACQUIRED
    assert not workspace.media_manifest_json.exists()
    assert not workspace.normalization_manifest_json.exists()
    assert not list(workspace.safe_path("normalized/staging").iterdir())


def test_stale_normalization_attempt_is_removed_before_new_work(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, _raw_artifact = _acquired_job(tmp_path, source, "stale-attempt")
    stale = workspace.safe_path("normalized/staging") / ("a" * 32)
    stale.mkdir()
    (stale / "partial.bin").write_bytes(b"partial")

    cleanup_stale_attempts(workspace)

    assert not stale.exists()


def test_stale_normalization_cleanup_rejects_direct_child_symlink(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, _raw_artifact = _acquired_job(tmp_path, source, "stale-link")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must remain", encoding="utf-8")
    unsafe = workspace.safe_path("normalized/staging") / ("b" * 32)
    try:
        os.symlink(external, unsafe, target_is_directory=True)
    except OSError:
        pytest.skip("the platform does not permit test symlink creation")

    with pytest.raises(VepError) as captured:
        cleanup_stale_attempts(workspace)

    assert captured.value.code == "NORMALIZATION_STAGING_INVALID"
    assert sentinel.read_text(encoding="utf-8") == "must remain"


def test_stale_normalization_cleanup_rejects_descendant_symlink(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, _raw_artifact = _acquired_job(tmp_path, source, "stale-nested-link")
    external = tmp_path / "external-nested"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must remain", encoding="utf-8")
    attempt = workspace.safe_path("normalized/staging") / ("d" * 32)
    attempt.mkdir()
    unsafe = attempt / "nested"
    try:
        os.symlink(external, unsafe, target_is_directory=True)
    except OSError:
        pytest.skip("the platform does not permit test symlink creation")

    with pytest.raises(VepError) as captured:
        cleanup_stale_attempts(workspace)

    assert captured.value.code == "NORMALIZATION_STAGING_INVALID"
    assert sentinel.read_text(encoding="utf-8") == "must remain"


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows-specific reparse point")
def test_stale_normalization_cleanup_rejects_direct_child_junction(tmp_path: Path) -> None:
    if not hasattr(Path, "is_junction"):
        pytest.skip("Path.is_junction is unavailable")
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, _raw_artifact = _acquired_job(tmp_path, source, "stale-junction")
    external = tmp_path / "external-junction"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must remain", encoding="utf-8")
    unsafe = workspace.safe_path("normalized/staging") / ("c" * 32)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(unsafe), str(external)],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not unsafe.is_junction():
        pytest.skip("the platform does not permit test junction creation")
    try:
        with pytest.raises(VepError) as captured:
            cleanup_stale_attempts(workspace)
        assert captured.value.code == "NORMALIZATION_STAGING_INVALID"
        assert sentinel.read_text(encoding="utf-8") == "must remain"
    finally:
        unsafe.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows-specific reparse point")
def test_stale_normalization_cleanup_rejects_descendant_junction(tmp_path: Path) -> None:
    if not hasattr(Path, "is_junction"):
        pytest.skip("Path.is_junction is unavailable")
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, _raw_artifact = _acquired_job(tmp_path, source, "snj")
    external = tmp_path / "external-nested-junction"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must remain", encoding="utf-8")
    attempt = workspace.safe_path("normalized/staging") / ("e" * 32)
    attempt.mkdir()
    unsafe = attempt / "nested"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(unsafe), str(external)],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not unsafe.is_junction():
        pytest.skip("the platform does not permit test junction creation")
    try:
        with pytest.raises(VepError) as captured:
            cleanup_stale_attempts(workspace)
        assert captured.value.code == "NORMALIZATION_STAGING_INVALID"
        assert sentinel.read_text(encoding="utf-8") == "must remain"
    finally:
        unsafe.rmdir()


@pytest.mark.parametrize("with_subtitle", [True])
def test_actual_synthetic_normalization_publish_recovery_reuse_and_tamper(
    tmp_path: Path,
    with_subtitle: bool,
) -> None:
    root = tmp_path / "m1"
    root.mkdir()
    source = _synthetic_source(root, audio=True, subtitle=with_subtitle)
    source_hash = sha256_file(source)
    workspace, raw_artifact = _acquired_job(root, source, "actual-media")
    config = NormalizationConfig()

    first = normalize_job(workspace.root, config)
    manifest = first.media_manifest
    assert first.reused is False
    assert manifest.analysis_proxy.codec_name == "h264"
    assert (manifest.analysis_proxy.width, manifest.analysis_proxy.height) == (640, 360)
    assert manifest.speech_audio is not None
    assert manifest.speech_audio.sample_rate_hz == 16000
    assert manifest.speech_audio.channels == 1
    assert manifest.subtitles[0].status is SubtitleStatus.NORMALIZED
    assert manifest.subtitles[0].provenance.value == "embedded"
    cues_artifact = manifest.subtitles[0].cues_artifact
    assert cues_artifact is not None
    cues = read_jsonl(workspace.safe_path(cues_artifact.relative_storage_path), SubtitleCue)
    assert [cue.text for cue in cues] == ["synthetic subtitle text"]
    # FFmpeg 6.1 and 9.0 choose slightly different Matroska time-base offsets
    # for this synthetic subtitle. The tool identity is fingerprinted, while
    # the normalized cue must preserve duration and remain tightly bounded.
    assert abs(cues[0].start_ms - 250) <= 50
    assert cues[0].end_ms - cues[0].start_ms == 1250
    oversized_subtitle_payload = manifest.model_dump(mode="json")
    oversized_subtitle_payload["subtitles"][0]["cues_artifact"]["size_bytes"] = (
        config.maximum_subtitle_bytes + 1
    )
    with pytest.raises(ValueError):
        MediaManifest.model_validate(oversized_subtitle_payload)
    oversized_audio_payload = manifest.model_dump(mode="json")
    audio_stream = next(
        stream
        for stream in oversized_audio_payload["probe"]["streams"]
        if stream["stream_type"] == "audio"
    )
    audio_stream["sample_rate_hz"] = config.maximum_source_audio_sample_rate_hz + 1
    with pytest.raises(ValueError):
        MediaManifest.model_validate(oversized_audio_payload)
    oversized_audio_payload = manifest.model_dump(mode="json")
    audio_stream = next(
        stream
        for stream in oversized_audio_payload["probe"]["streams"]
        if stream["stream_type"] == "audio"
    )
    audio_stream["channels"] = config.maximum_source_audio_channels + 1
    with pytest.raises(ValueError):
        MediaManifest.model_validate(oversized_audio_payload)
    assert manifest.frame_index.records
    assert all(
        0 <= record.timestamp_ms <= manifest.probe.duration_ms
        for record in manifest.frame_index.records
    )
    assert sha256_file(source) == source_hash
    assert sha256_file(workspace.safe_path(raw_artifact.relative_storage_path)) == source_hash

    class NoToolCalls:
        def __getattribute__(self, name: str):
            raise AssertionError(f"unexpected media tool access: {name}")

    reused = normalize_job(workspace.root, config, runner_override=NoToolCalls())  # type: ignore[arg-type]
    assert reused.reused is True
    cli_result = CliRunner().invoke(app, ["normalize", str(workspace.root), "--json"])
    assert cli_result.exit_code == 0
    cli_payload = json.loads(cli_result.stdout)
    assert cli_payload["state"] == "NORMALIZED"
    assert cli_payload["reused"] is True
    rejected_args = CliRunner().invoke(
        app,
        ["normalize", str(workspace.root), "--ffmpeg-arg", "unsafe", "--json"],
    )
    assert rejected_args.exit_code == 2
    assert "unsafe" not in rejected_args.stdout
    assert "unsafe" not in rejected_args.stderr

    records = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    workspace.transitions_jsonl.write_bytes(
        b"".join(canonical_json_bytes(record) + b"\n" for record in records[:-1])
    )
    workspace.normalization_manifest_json.unlink()
    recovered = normalize_job(
        workspace.root,
        config,
        runner_override=NoToolCalls(),  # type: ignore[arg-type]
    )
    assert recovered.reused is True
    assert workspace.normalization_manifest_json.exists()

    proxy_path = workspace.safe_path(manifest.analysis_proxy.artifact.relative_storage_path)
    proxy_path.write_bytes(proxy_path.read_bytes() + b"tamper")
    with pytest.raises(VepError) as captured:
        normalize_job(workspace.root, config, runner_override=NoToolCalls())  # type: ignore[arg-type]
    assert captured.value.code == "NORMALIZED_BUNDLE_TAMPERED"
    assert sha256_file(source) == source_hash


def test_actual_srt_markup_is_normalized_without_source_text_preservation_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "m-markup"
    root.mkdir()
    source = _synthetic_source(
        root,
        audio=False,
        subtitle=True,
        subtitle_payload=("1\n00:00:00,250 --> 00:00:01,500\n{\\an8}<i>styled subtitle</i>\n"),
    )
    workspace, _raw_artifact = _acquired_job(root, source, "normalized-srt-markup")

    result = normalize_job(workspace.root, NormalizationConfig())

    subtitle = result.media_manifest.subtitles[0]
    assert subtitle.status is SubtitleStatus.NORMALIZED
    assert subtitle.raw_text_preserved is False
    assert subtitle.cues_artifact is not None


def test_normalized_validate_and_status_require_the_stage_manifest(tmp_path: Path) -> None:
    root = tmp_path / "m4"
    root.mkdir()
    source = _synthetic_source(root, audio=False, subtitle=False)
    workspace, _raw_artifact = _acquired_job(root, source, "normalized-stage-integrity")
    result = normalize_job(workspace.root, NormalizationConfig())

    workspace.normalization_manifest_json.unlink()
    with pytest.raises(VepError) as captured:
        validate_job(workspace.root)
    assert captured.value.code == "STAGE_MANIFEST_INVALID"
    status, exit_code = job_status(workspace.root)
    assert exit_code != 0
    assert status["normalization_integrity"] == {
        "status": "invalid",
        "error_code": "STAGE_MANIFEST_INVALID",
    }
    status_cli = CliRunner().invoke(app, ["job", "status", str(workspace.root), "--json"])
    assert status_cli.exit_code != 0
    assert json.loads(status_cli.stdout)["normalization_integrity"]["error_code"] == (
        "STAGE_MANIFEST_INVALID"
    )

    stage_payload = result.stage_manifest.model_dump(mode="json")
    stage_payload["warnings"] = ["tampered"]
    atomic_write_json(workspace.normalization_manifest_json, stage_payload, private=True)
    with pytest.raises(VepError) as captured:
        validate_job(workspace.root)
    assert captured.value.code == "STAGE_MANIFEST_MISMATCH"
    status, exit_code = job_status(workspace.root)
    assert exit_code != 0
    assert status["normalization_integrity"]["error_code"] == "STAGE_MANIFEST_MISMATCH"
    validate_cli = CliRunner().invoke(app, ["job", "validate", str(workspace.root), "--json"])
    assert validate_cli.exit_code != 0
    assert json.loads(validate_cli.stdout)["error"]["code"] == "STAGE_MANIFEST_MISMATCH"


def test_actual_synthetic_video_without_audio_or_subtitle_is_frame_only_success(
    tmp_path: Path,
) -> None:
    root = tmp_path / "m2"
    root.mkdir()
    source = _synthetic_source(root, audio=False, subtitle=False)
    workspace, _raw_artifact = _acquired_job(root, source, "frame-only")
    result = normalize_job(
        workspace.root,
        NormalizationConfig(periodic_frame_interval_seconds=1),
    )
    assert result.media_manifest.speech_audio is None
    assert result.media_manifest.subtitles == []
    assert result.media_manifest.warnings == ["AUDIO_STREAM_UNAVAILABLE"]
    assert result.media_manifest.frame_index.records


def test_actual_sparse_video_records_observed_frame_pts_not_requested_seek(
    tmp_path: Path,
) -> None:
    root = tmp_path / "m3"
    root.mkdir()
    source = _synthetic_source(root, audio=False, subtitle=False, frame_rate=2)
    workspace, _raw_artifact = _acquired_job(root, source, "sparse-frame-pts")

    result = normalize_job(
        workspace.root,
        NormalizationConfig(
            # At 2 fps this keeps every requested point before the final
            # decodable source PTS, while still requiring observed PTS to
            # diverge from requested candidates.
            periodic_frame_interval_seconds=0.6,
            scene_change_threshold=0.99,
        ),
    )

    records = result.media_manifest.frame_index.records
    assert records
    assert all(
        0 <= record.timestamp_ms <= result.media_manifest.probe.duration_ms for record in records
    )
    assert any(record.timestamp_ms not in record.candidate_timestamps_ms for record in records)


def test_normalization_releases_job_lock_and_revalidates_raw_after_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, raw_artifact = _acquired_job(tmp_path, source, "snapshot-check")

    class StubRunner:
        def discover_identities(self, **_: object):
            return _synthetic_identities()

        def pin_identities(self, *_args: object) -> None:
            return None

        def revalidate_pinned_identities(self, *_args: object) -> None:
            return None

    def mutate_during_processing(*_args: object, **_kwargs: object):
        from video_evidence_pipeline.storage.jsonl import exclusive_append_lock

        with exclusive_append_lock(workspace.transitions_jsonl):
            pass
        workspace.safe_path(raw_artifact.relative_storage_path).write_bytes(b"changed")
        return object()

    monkeypatch.setattr(application_module, "process_media", mutate_during_processing)
    with pytest.raises(VepError) as captured:
        normalize_job(workspace.root, runner_override=StubRunner())  # type: ignore[arg-type]
    assert captured.value.code == "ARTIFACT_INTEGRITY_FAILED"
    assert not workspace.media_manifest_json.exists()
    assert not list(workspace.safe_path("normalized/staging").iterdir())


def test_normalization_revalidates_pinned_tools_immediately_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, _raw_artifact = _acquired_job(tmp_path, source, "tib")
    calls: list[str] = []

    class ChangedToolRunner:
        def discover_identities(self, **_: object):
            return _synthetic_identities()

        def pin_identities(self, *_args: object) -> None:
            calls.append("pin")

        def revalidate_pinned_identities(self, *_args: object) -> None:
            calls.append("revalidate")
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_CHANGED", "media tool changed during normalization"
            )

    monkeypatch.setattr(
        application_module,
        "process_media",
        lambda *_args, **_kwargs: SimpleNamespace(normalization_fingerprint="c" * 64),
    )
    monkeypatch.setattr(application_module, "recover_normalization_result", lambda *_a, **_k: None)
    monkeypatch.setattr(
        application_module,
        "publish_staged_media",
        lambda *_a, **_k: pytest.fail("publication must not follow tool replacement"),
    )

    with pytest.raises(VepError) as captured:
        normalize_job(workspace.root, runner_override=ChangedToolRunner())  # type: ignore[arg-type]

    assert captured.value.code == "MEDIA_TOOL_IDENTITY_CHANGED"
    assert calls == ["pin", "revalidate"]
    assert not workspace.media_manifest_json.exists()


def test_concurrent_normalization_keeps_active_attempt_and_publishes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "n"
    root.mkdir()
    source = root / "s.bin"
    source.write_bytes(b"synthetic raw bytes")
    workspace, _raw_artifact = _acquired_job(root, source, "concurrent-normalize")
    processing_started = threading.Event()
    allow_completion = threading.Event()
    active_attempts: list[Path] = []
    publication_count = 0
    publication_guard = threading.Lock()
    expected_fingerprint = "c" * 64
    fake_result = SimpleNamespace(
        media_manifest=SimpleNamespace(normalization_fingerprint=expected_fingerprint),
        reused=False,
    )

    class StubRunner:
        def discover_identities(self, **_: object):
            return _synthetic_identities()

        def pin_identities(self, *_args: object) -> None:
            return None

        def revalidate_pinned_identities(self, *_args: object) -> None:
            return None

    def fake_process(*args: object, **_kwargs: object):
        active_attempts.append(args[4])
        processing_started.set()
        assert allow_completion.wait(timeout=5)
        return SimpleNamespace(normalization_fingerprint=expected_fingerprint)

    def fake_publish(*_args: object, **_kwargs: object):
        nonlocal publication_count
        with publication_guard:
            publication_count += 1
        return fake_result

    monkeypatch.setattr(application_module, "process_media", fake_process)
    monkeypatch.setattr(application_module, "publish_staged_media", fake_publish)
    monkeypatch.setattr(application_module, "recover_normalization_result", lambda *_a, **_k: None)
    monkeypatch.setattr(
        application_module,
        "load_normalization_result",
        lambda *_a, **_k: fake_result,
    )
    results: list[object] = []
    failures: list[BaseException] = []

    def call_normalize() -> None:
        try:
            results.append(normalize_job(workspace.root, runner_override=StubRunner()))  # type: ignore[arg-type]
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=call_normalize)
    thread.start()
    assert processing_started.wait(timeout=5)
    assert active_attempts[0].is_dir()
    with pytest.raises(VepError) as captured:
        normalize_job(workspace.root, runner_override=StubRunner())  # type: ignore[arg-type]
    assert captured.value.code == "NORMALIZATION_BUSY"
    assert active_attempts[0].is_dir()
    allow_completion.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    assert len(results) == 1
    assert publication_count == 1
    records = read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    assert sum(record.to_state is JobState.NORMALIZED for record in records) == 1
    assert not list(workspace.safe_path("normalized/staging").iterdir())
