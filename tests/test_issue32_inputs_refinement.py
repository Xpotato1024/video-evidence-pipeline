from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import video_evidence_pipeline.media.normalization as normalization_module
import video_evidence_pipeline.supporting as supporting_module
import video_evidence_pipeline.transcription.refinement as refinement_module
from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    initialize_job,
    normalize_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.evidence import (
    ATOMIC_PROPOSITION_RULE,
    AnalyzerExecutionMode,
    AnalyzerIdentity,
    EvidenceConfidence,
    EvidenceRecord,
    EvidenceResponseManifest,
    InterpretationLevel,
    SourceRef,
    SourceRefKind,
)
from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    PrimaryInputKind,
    PrimaryInputSpec,
    SourceKind,
    SourceSpec,
    SupportingInputKind,
    SupportingInputSpec,
    job_fingerprint,
    job_to_yaml,
)
from video_evidence_pipeline.domain.media import (
    MediaManifest,
    MediaProbe,
    MediaRational,
    MediaStream,
    MediaStreamType,
    NormalizationConfig,
    SpeechAudio,
    ToolIdentity,
)
from video_evidence_pipeline.domain.refinement import (
    RefinementAmbiguity,
    RefinementEdit,
    RefinementEditKind,
    RefinementExecutionMode,
    RefinementResponseManifest,
    RefinerIdentity,
)
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.domain.transcription import (
    AsrSegment,
    TranscriptConfidence,
    TranscriptConfidenceKind,
    TranscriptionBackendIdentity,
    TranscriptionConfig,
    TranscriptSource,
)
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.evidence import pipeline as evidence_module
from video_evidence_pipeline.media.normalization import (
    AudioProbeInfo,
    process_media,
    transcript_primary_normalization_fingerprint,
)
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.supporting import prepare_supporting_inputs
from video_evidence_pipeline.transcription.pipeline import (
    create_attempt_directory as create_transcription_attempt,
)
from video_evidence_pipeline.transcription.pipeline import (
    prepare_supplied_transcript,
    prepare_transcript,
    publish_transcript,
)
from video_evidence_pipeline.transcription.refinement import (
    load_refinement_result,
    prepare_refinement_request,
    publish_refinement,
)

_NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _job(*, supplied: bool = False, notes: Path | None = None) -> JobSpec:
    return JobSpec(
        job_id="issue32-job",
        created_at=_NOW,
        source=SourceSpec(kind=SourceKind.LOCAL_FILE, locator="source.txt"),
        primary_input=PrimaryInputSpec(kind=PrimaryInputKind.TRANSCRIPT) if supplied else None,
        supporting_inputs=(
            [SupportingInputSpec(kind=SupportingInputKind.NOTES, locator=str(notes))]
            if notes is not None
            else []
        ),
        assignment=AssignmentSpec(task_type="report", language="ja"),
        output=OutputSpec(format="markdown"),
    )


def _raw_artifact(workspace: JobWorkspace, payload: bytes) -> ArtifactRef:
    path = workspace.safe_path("source/raw/source.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactRef(
        artifact_id="raw-source",
        artifact_type="raw_source",
        content_sha256=sha256_bytes(payload),
        size_bytes=len(payload),
        relative_storage_path="source/raw/source.txt",
    )


def _transcript_media(job: JobSpec, raw: ArtifactRef) -> MediaManifest:
    config = NormalizationConfig()
    return MediaManifest(
        job_id=job.job_id,
        normalization_fingerprint=transcript_primary_normalization_fingerprint(raw, config),
        implementation_version="media-normalization-v1",
        configuration=config,
        primary_input_kind=PrimaryInputKind.TRANSCRIPT,
        raw_artifact=raw,
        warnings=["TRANSCRIPT_PRIMARY"],
        started_at=_NOW,
        created_at=_NOW,
    )


def _identities() -> tuple[ToolIdentity, ToolIdentity]:
    return (
        ToolIdentity(name="ffmpeg", version="test", executable_sha256="a" * 64),
        ToolIdentity(name="ffprobe", version="test", executable_sha256="b" * 64),
    )


def test_v1_job_fingerprint_and_yaml_remain_legacy_shaped() -> None:
    job = _job()
    legacy = {
        "schema_version": "1.0",
        "job_id": job.job_id,
        "created_at": job.model_dump(mode="json")["created_at"],
        "source": job.source.model_dump(mode="json"),
        "assignment": job.assignment.model_dump(mode="json"),
        "output": job.output.model_dump(mode="json"),
    }

    assert job_fingerprint(job) == fingerprint(legacy)
    yaml = job_to_yaml(job)
    assert "primary_input" not in yaml
    assert "supporting_inputs" not in yaml


def test_supplied_transcript_application_path_reaches_raw_transcript_without_asr(
    tmp_path: Path,
) -> None:
    source = tmp_path / "supplied.md"
    source.write_text("第一段落。\n\n第二段落。\n", encoding="utf-8")
    workspace, _job_spec = initialize_job(
        tmp_path,
        job_id="supplied-application-job",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="ja",
        output_format="markdown",
        primary_input=PrimaryInputKind.TRANSCRIPT,
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic transcript",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )

    acquire_job(workspace.root)
    normalized = normalize_job(workspace.root)
    transcript = transcribe_job(workspace.root)

    assert normalized.media_manifest.primary_input_kind is PrimaryInputKind.TRANSCRIPT
    assert normalized.media_manifest.ffmpeg is None
    assert [item.source for item in transcript.segments] == [
        TranscriptSource.SUPPLIED,
        TranscriptSource.SUPPLIED,
    ]
    assert all(item.start_ms is None and item.end_ms is None for item in transcript.segments)
    assert validate_job(workspace.root)["state"] == "TRANSCRIBED"


def test_audio_primary_only_stages_speech_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "input.wav"
    source.write_bytes(b"source")
    probe = MediaProbe(
        format_name="wav",
        duration_ms=1_000,
        streams=[
            MediaStream(
                index=0,
                stream_type=MediaStreamType.AUDIO,
                codec_name="pcm_s16le",
                time_base=MediaRational(numerator=1, denominator=1_000),
                duration_ms=1_000,
                sample_rate_hz=16_000,
                channels=1,
            )
        ],
    )
    monkeypatch.setattr(normalization_module, "probe_media", lambda *_args: probe)
    monkeypatch.setattr(
        normalization_module,
        "_run_speech_audio",
        lambda _runner, _source, target, _config: target.write_bytes(b"speech"),
    )
    monkeypatch.setattr(
        normalization_module, "_verify_staged_output", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        normalization_module,
        "_audio_probe",
        lambda *_args: AudioProbeInfo("wav", "pcm_s16le", 1_000, 16_000, 1),
    )
    ffmpeg, ffprobe = _identities()
    raw = ArtifactRef(
        artifact_id="raw-audio",
        artifact_type="raw_source",
        content_sha256="c" * 64,
        size_bytes=1,
        relative_storage_path="source/raw/input.wav",
    )
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    staged = process_media(
        object(),
        source,
        raw,
        NormalizationConfig(),
        attempt,
        ffmpeg,
        ffprobe,
        primary_input_kind=PrimaryInputKind.AUDIO,
    )

    assert staged.analysis_path is None
    assert staged.analysis_probe is None
    assert staged.frames == ()
    assert staged.subtitles == ()
    assert staged.audio_path is not None


class _AudioAsr:
    def __init__(self) -> None:
        self.called = False

    def identity(self) -> TranscriptionBackendIdentity:
        return TranscriptionBackendIdentity(
            source=TranscriptSource.ASR,
            name="audio-test-asr",
            version="1",
            runtime="fixture",
            model="fixture",
            model_fingerprint="f" * 64,
            local_files_only=True,
        )

    def transcribe(self, audio_path: Path, *, language, intervals, configuration):
        del language, configuration
        self.called = audio_path.is_file() and len(intervals) == 1
        return [
            AsrSegment(
                start_ms=0,
                end_ms=1_000,
                raw_text="audio raw transcript",
                backend_segment_id="audio-1",
                confidence=TranscriptConfidence(
                    kind=TranscriptConfidenceKind.BACKEND_METRIC,
                    avg_logprob=-0.1,
                    no_speech_probability=0.0,
                ),
            )
        ]


def test_audio_primary_transcribes_speech_audio_to_raw_transcript(tmp_path: Path) -> None:
    job = JobSpec(
        job_id="audio-transcript-job",
        created_at=_NOW,
        source=SourceSpec(kind=SourceKind.LOCAL_FILE, locator="audio.wav"),
        primary_input=PrimaryInputSpec(kind=PrimaryInputKind.AUDIO),
        assignment=AssignmentSpec(task_type="report", language="ja"),
        output=OutputSpec(format="markdown"),
    )
    workspace = JobWorkspace.create(tmp_path, job.job_id)
    raw = _raw_artifact(workspace, b"raw-audio")
    speech_payload = b"normalized-speech"
    speech_path = workspace.safe_path("normalized/audio/speech.wav")
    speech_path.parent.mkdir(parents=True, exist_ok=True)
    speech_path.write_bytes(speech_payload)
    speech_artifact = ArtifactRef(
        artifact_id="speech-audio",
        artifact_type="normalized_speech_audio",
        content_sha256=sha256_bytes(speech_payload),
        size_bytes=len(speech_payload),
        relative_storage_path="normalized/audio/speech.wav",
    )
    ffmpeg, ffprobe = _identities()
    media = MediaManifest(
        job_id=job.job_id,
        normalization_fingerprint="9" * 64,
        implementation_version="media-normalization-v1",
        configuration=NormalizationConfig(),
        primary_input_kind=PrimaryInputKind.AUDIO,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        raw_artifact=raw,
        probe=MediaProbe(
            format_name="wav",
            duration_ms=1_000,
            streams=[
                MediaStream(
                    index=0,
                    stream_type=MediaStreamType.AUDIO,
                    codec_name="pcm_s16le",
                    time_base=MediaRational(numerator=1, denominator=1_000),
                    duration_ms=1_000,
                    sample_rate_hz=16_000,
                    channels=1,
                )
            ],
        ),
        speech_audio=SpeechAudio(
            artifact=speech_artifact,
            format_name="wav",
            codec_name="pcm_s16le",
            sample_rate_hz=16_000,
            channels=1,
            duration_ms=1_000,
        ),
        started_at=_NOW,
        created_at=_NOW,
    )
    backend = _AudioAsr()

    prepared = prepare_transcript(
        workspace,
        job,
        media,
        TranscriptionConfig(minimum_caption_coverage=0.9),
        backend,
        model_directory=None,
    )

    assert backend.called is True
    assert [item.source for item in prepared.segments] == [TranscriptSource.ASR]
    assert prepared.segments[0].raw_text == "audio raw transcript"


def test_supplied_transcript_refines_separately_and_rejects_ungrounded_merge(
    tmp_path: Path,
) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("創立100周年と明記されている。\n", encoding="utf-8")
    job = _job(supplied=True, notes=notes)
    workspace = JobWorkspace.create(tmp_path, job.job_id)
    raw_bytes = "100周年\n\n5月31日の試合\n".encode()
    raw = _raw_artifact(workspace, raw_bytes)
    media = _transcript_media(job, raw)
    prepared = prepare_supplied_transcript(workspace, job, media, TranscriptionConfig())
    transcript = publish_transcript(
        workspace,
        job,
        media,
        prepared,
        create_transcription_attempt(workspace),
    )

    assert [segment.source for segment in transcript.segments] == [
        TranscriptSource.SUPPLIED,
        TranscriptSource.SUPPLIED,
    ]
    assert all(
        segment.start_ms is None and segment.end_ms is None for segment in transcript.segments
    )
    assert workspace.safe_path(raw.relative_storage_path).read_bytes() == raw_bytes

    request = prepare_refinement_request(
        workspace,
        job,
        media,
        transcript.transcript_manifest,
        transcript.segments,
    ).request
    transcript_ref = next(
        item
        for item in request.available_source_refs
        if item.kind is SourceRefKind.TRANSCRIPT_SEGMENT and item.ref_id == "T000001"
    )
    notes_ref = next(
        item for item in request.available_source_refs if item.kind is SourceRefKind.NOTES
    )

    with pytest.raises(ValidationError):
        RefinementEdit(
            edit_id="R000001",
            segment_id="T000001",
            original_segment_fingerprint=fingerprint(transcript.segments[0]),
            refined_text="100周年 5月31日の試合",
            kind=RefinementEditKind.SEMANTIC,
            source_refs=[transcript_ref],
        )

    response = RefinementResponseManifest(
        request_fingerprint=request.request_fingerprint,
        refiner=RefinerIdentity(
            provider="fixture",
            model="fixture",
            version="1",
            executor="pytest",
            execution_mode=RefinementExecutionMode.TEST_FIXTURE,
            external_api_used=False,
        ),
        semantic_edits=[
            RefinementEdit(
                edit_id="R000001",
                segment_id="T000001",
                original_segment_fingerprint=fingerprint(transcript.segments[0]),
                refined_text="創立100周年",
                kind=RefinementEditKind.SEMANTIC,
                source_refs=sorted(
                    [notes_ref, transcript_ref], key=lambda item: (item.kind.value, item.ref_id)
                ),
            )
        ],
        ambiguities=[
            RefinementAmbiguity(
                ambiguity_id="RA000001",
                segment_id="T000002",
                description="No supporting source resolves whether this game is related.",
                source_refs=sorted(
                    [
                        transcript_ref,
                        next(
                            item
                            for item in request.available_source_refs
                            if item.kind is SourceRefKind.TRANSCRIPT_SEGMENT
                            and item.ref_id == "T000002"
                        ),
                    ],
                    key=lambda item: (item.kind.value, item.ref_id),
                ),
            )
        ],
        created_at=request.created_at + timedelta(seconds=1),
    )
    result = publish_refinement(
        workspace,
        job,
        transcript.transcript_manifest,
        transcript.segments,
        request,
        response,
    )
    loaded = load_refinement_result(
        workspace,
        job,
        media,
        transcript.transcript_manifest,
        transcript.segments,
        request=request,
    )

    assert result.segments[0].normalized_text == "創立100周年"
    assert "5月31日" not in result.segments[0].normalized_text
    assert loaded.segments == result.segments
    assert transcript.segments[0].normalized_text == "100周年"
    assert workspace.safe_path(raw.relative_storage_path).read_bytes() == raw_bytes
    assert loaded.ambiguities[0].segment_id == "T000002"
    evidence_request = evidence_module._expected_request(
        workspace,
        job,
        media,
        refinement_module.evidence_transcript_projection(transcript.transcript_manifest, loaded),
        loaded.segments,
        created_at=_NOW,
    )
    assert evidence_request.analyzer_contract.atomic_proposition_contract == (
        ATOMIC_PROPOSITION_RULE
    )
    assert [item.description for item in evidence_request.unresolved_ambiguities] == [
        "No supporting source resolves whether this game is related."
    ]
    assert (
        evidence_request.unresolved_ambiguities[0].source_refs[0]
        == (evidence_request.transcript_refs[0])
    )
    response = EvidenceResponseManifest(
        request_fingerprint=evidence_request.request_fingerprint,
        analyzer=AnalyzerIdentity(
            provider="pytest",
            model="deterministic",
            version="1",
            executor="pytest",
            execution_mode=AnalyzerExecutionMode.TEST_FIXTURE,
            external_api_used=False,
        ),
        evidence_records=[],
        ambiguity_records=list(evidence_request.unresolved_ambiguities),
        created_at=evidence_request.created_at + timedelta(seconds=1),
    )
    evidence_module._validate_response(evidence_request, response, allow_fixture=True)
    with pytest.raises(VepError) as dropped:
        evidence_module._validate_response(
            evidence_request,
            response.model_copy(update={"ambiguity_records": []}),
            allow_fixture=True,
        )
    assert dropped.value.code == "UNRESOLVED_AMBIGUITY_LOST"


def test_evidence_record_keeps_one_structural_proposition_line_and_notes_non_video() -> None:
    transcript_ref = SourceRef(
        kind=SourceRefKind.TRANSCRIPT_SEGMENT,
        ref_id="T000001",
        content_sha256="d" * 64,
    )
    notes_ref = SourceRef(
        kind=SourceRefKind.NOTES,
        ref_id="S000001",
        content_sha256="e" * 64,
    )
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id="E000001",
            observation="100周年\n5月31日の試合",
            interpretation_level=InterpretationLevel.EXPLICIT_STATEMENT,
            confidence=EvidenceConfidence.MEDIUM,
            source_refs=[transcript_ref],
        )
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id="E000001",
            observation="The notes say so.",
            interpretation_level=InterpretationLevel.EXPLICIT_STATEMENT,
            confidence=EvidenceConfidence.MEDIUM,
            source_refs=sorted(
                [notes_ref, transcript_ref], key=lambda item: (item.kind.value, item.ref_id)
            ),
            asserted_as_video_fact=True,
        )


def test_pdf_and_image_supporting_inputs_keep_validated_originals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "support.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"bounded-image-fixture")
    pdf = tmp_path / "support.pdf"
    pdf_payload = b"%PDF-1.4\n(embedded support) Tj\n%%EOF\n"
    pdf.write_bytes(pdf_payload)
    rendered_payloads: list[bytes] = []

    def render_snapshot(_workspace: JobWorkspace, payload: bytes) -> None:
        rendered_payloads.append(payload)
        pdf.write_bytes(b"%PDF-1.4\n(replaced after snapshot) Tj\n%%EOF\n")
        return None

    monkeypatch.setattr(supporting_module, "_render_first_pdf_page", render_snapshot)
    job = JobSpec(
        job_id="supporting-kinds-job",
        created_at=_NOW,
        source=SourceSpec(kind=SourceKind.LOCAL_FILE, locator="source.txt"),
        primary_input=PrimaryInputSpec(kind=PrimaryInputKind.TRANSCRIPT),
        supporting_inputs=[
            SupportingInputSpec(kind=SupportingInputKind.IMAGE, locator=str(image)),
            SupportingInputSpec(kind=SupportingInputKind.PDF, locator=str(pdf)),
        ],
        assignment=AssignmentSpec(task_type="report", language="ja"),
        output=OutputSpec(format="markdown"),
    )
    workspace = JobWorkspace.create(tmp_path, job.job_id)

    manifest = prepare_supporting_inputs(workspace, job)

    assert [item.kind for item in manifest.inputs] == [
        SupportingInputKind.IMAGE,
        SupportingInputKind.PDF,
    ]
    assert manifest.inputs[0].original_artifact.artifact_type == "supporting_image_original"
    assert manifest.inputs[1].canonical_text_artifact is not None
    assert rendered_payloads == [pdf_payload]
    assert (
        workspace.safe_path(manifest.inputs[1].original_artifact.relative_storage_path).read_bytes()
        == pdf_payload
    )
    assert (
        workspace.safe_path(
            manifest.inputs[1].canonical_text_artifact.relative_storage_path
        ).read_text(encoding="utf-8")
        == "embedded support\n"
    )
    raw = _raw_artifact(workspace, b"support-catalog")
    refinement_refs = refinement_module._source_catalog(_transcript_media(job, raw), (), manifest)[
        1
    ]
    evidence_refs = evidence_module._supporting_catalog(workspace, job)[1]
    assert any(item.kind is SourceRefKind.APPROVED_ARTIFACT for item in refinement_refs)
    assert any(item.kind is SourceRefKind.APPROVED_ARTIFACT for item in evidence_refs)

    invalid_image = tmp_path / "invalid.png"
    invalid_image.write_bytes(b"not-an-image")
    invalid_job = job.model_copy(
        update={
            "job_id": "invalid-supporting-job",
            "supporting_inputs": [
                SupportingInputSpec(kind=SupportingInputKind.IMAGE, locator=str(invalid_image))
            ],
        }
    )
    invalid_workspace = JobWorkspace.create(tmp_path, invalid_job.job_id)
    with pytest.raises(VepError, match="invalid signature"):
        prepare_supporting_inputs(invalid_workspace, invalid_job)
