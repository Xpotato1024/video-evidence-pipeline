"""Fixed evidence request preparation and fail-closed semantic response import."""

from __future__ import annotations

import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline import __version__
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRef,
    EvidenceLedgerArtifactRef,
    ProducerIdentity,
    ProducerKind,
)
from video_evidence_pipeline.domain.evidence import (
    ATOMIC_PROPOSITION_RULE,
    AmbiguityLedger,
    AmbiguityRecord,
    AmbiguityStatus,
    AnalyzerContract,
    AnalyzerExecutionMode,
    AnalyzerIdentity,
    AssignmentRequirement,
    EvidenceAssignmentContext,
    EvidenceManifest,
    EvidenceRecord,
    EvidenceRequestManifest,
    EvidenceResponseManifest,
    InterpretationLevel,
    ReviewStatus,
    SourceRef,
    SourceRefKind,
)
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.job import JobSpec, requires_transcript_refinement
from video_evidence_pipeline.domain.manifests import StageManifest, StageStatus
from video_evidence_pipeline.domain.media import FrameRecord, MediaManifest
from video_evidence_pipeline.domain.serialization import (
    canonical_json_bytes,
    fingerprint,
    pretty_json_bytes,
)
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.supporting import SupportingInputManifest
from video_evidence_pipeline.domain.transcription import TranscriptManifest, TranscriptSegment
from video_evidence_pipeline.errors import integrity_error, state_error
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
from video_evidence_pipeline.supporting import load_supporting_inputs
from video_evidence_pipeline.transcription.pipeline import (
    load_transcription_result,
    media_bundle_fingerprint,
)
from video_evidence_pipeline.transcription.refinement import (
    evidence_transcript_projection,
    load_refinement_result,
)

EVIDENCE_IMPLEMENTATION_VERSION = "evidence-v1"
_ATTEMPT_NAME = re.compile(r"^[0-9a-f]{32}$")
_MAXIMUM_REQUEST_BYTES = 512 * 1024 * 1024
_MAXIMUM_RESPONSE_BYTES = 128 * 1024 * 1024
_MAXIMUM_LEDGER_BYTES = 512 * 1024 * 1024
_FIXTURE_MARKERS = frozenset({"deterministic", "fake", "fixture", "mock", "synthetic", "test"})
_METRIC_AVAILABILITY_FLAGS = frozenset(
    {"no_speech_probability_reported", "word_probabilities_reported"}
)
_MAXIMUM_AMBIGUITIES = 100_000


@dataclass(frozen=True, slots=True)
class EvidencePrepareResult:
    request: EvidenceRequestManifest
    reused: bool


@dataclass(frozen=True, slots=True)
class EvidenceImportResult:
    evidence_manifest: EvidenceManifest
    stage_manifest: StageManifest
    evidence_records: tuple[EvidenceRecord, ...]
    ambiguity_records: tuple[AmbiguityRecord, ...]
    reused: bool


def _read_bounded_model[ModelT: BaseModel](
    path: Path,
    model: type[ModelT],
    *,
    maximum_bytes: int,
    code: str,
) -> ModelT:
    try:
        before = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise integrity_error(code, "machine artifact is invalid")
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError:
        raise integrity_error(code, "machine artifact is unavailable") from None
    if len(payload) != before.st_size or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise integrity_error(code, "machine artifact changed while reading")
    try:
        return model.model_validate_json(payload, strict=True)
    except (UnicodeError, ValidationError):
        raise integrity_error(code, "machine artifact is malformed") from None


def _transcript_ref(segment: TranscriptSegment) -> SourceRef:
    return SourceRef(
        kind=SourceRefKind.TRANSCRIPT_SEGMENT,
        ref_id=segment.segment_id,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        content_sha256=fingerprint(segment),
    )


def _frame_refs(media_manifest: MediaManifest) -> list[SourceRef]:
    if media_manifest.frame_index is None:
        return []
    return [
        SourceRef(
            kind=SourceRefKind.FRAME,
            ref_id=record.frame_id,
            start_ms=record.timestamp_ms,
            content_sha256=record.artifact.content_sha256,
        )
        for record in media_manifest.frame_index.records
    ]


def _request_ambiguities(
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
) -> list[AmbiguityRecord]:
    refs = {item.segment_id: _transcript_ref(item) for item in segments}
    records: list[AmbiguityRecord] = []

    def add(
        description: str,
        start_ms: int | None,
        end_ms: int | None,
        source_refs: list[SourceRef],
    ) -> None:
        if len(records) >= _MAXIMUM_AMBIGUITIES:
            raise state_error(
                "EVIDENCE_AMBIGUITY_LIMIT_EXCEEDED",
                "transcript ambiguities exceed the evidence request limit",
            )
        records.append(
            AmbiguityRecord(
                ambiguity_id=f"A{len(records) + 1:06d}",
                description=description,
                start_ms=start_ms,
                end_ms=end_ms,
                source_refs=source_refs,
                status=AmbiguityStatus.UNRESOLVED,
            )
        )

    for gap in transcript_manifest.gaps:
        adjacent = [
            _transcript_ref(item)
            for item in segments
            if item.end_ms == gap.start_ms or item.start_ms == gap.end_ms
        ]
        add("Transcript coverage gap", gap.start_ms, gap.end_ms, adjacent)
    for overlap in transcript_manifest.overlaps:
        add(
            "Transcript segment overlap",
            overlap.start_ms,
            overlap.end_ms,
            [refs[item] for item in overlap.segment_ids],
        )
    for segment in segments:
        for flag in segment.flags:
            if flag in _METRIC_AVAILABILITY_FLAGS:
                continue
            add(
                f"Transcript segment flag: {flag}",
                segment.start_ms,
                segment.end_ms,
                [refs[segment.segment_id]],
            )
    return records


def _refinement_ambiguities(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
    *,
    first_id: int,
) -> list[AmbiguityRecord]:
    if not requires_transcript_refinement(job):
        return []
    raw = load_transcription_result(workspace, job, media_manifest)
    refinement = load_refinement_result(
        workspace,
        job,
        media_manifest,
        raw.transcript_manifest,
        raw.segments,
    )
    if (
        evidence_transcript_projection(raw.transcript_manifest, refinement) != transcript_manifest
        or refinement.segments != segments
    ):
        raise integrity_error(
            "REFINEMENT_EVIDENCE_INPUT_MISMATCH",
            "evidence input does not match the canonical refined transcript",
        )
    by_id = {item.segment_id: item for item in segments}
    if first_id + len(refinement.ambiguities) - 1 > _MAXIMUM_AMBIGUITIES:
        raise state_error(
            "EVIDENCE_AMBIGUITY_LIMIT_EXCEEDED",
            "transcript ambiguities exceed the evidence request limit",
        )
    return [
        AmbiguityRecord(
            ambiguity_id=f"A{first_id + index:06d}",
            description=item.description,
            start_ms=by_id[item.segment_id].start_ms,
            end_ms=by_id[item.segment_id].end_ms,
            source_refs=sorted(
                [
                    _transcript_ref(by_id[source.ref_id])
                    if source.kind is SourceRefKind.TRANSCRIPT_SEGMENT
                    else source
                    for source in item.source_refs
                ],
                key=lambda source: (source.kind.value, source.ref_id),
            ),
            status=AmbiguityStatus.UNRESOLVED,
        )
        for index, item in enumerate(refinement.ambiguities)
    ]


def _supporting_catalog(
    workspace: JobWorkspace, job: JobSpec
) -> tuple[tuple[ArtifactRef, ...], tuple[SourceRef, ...]]:
    if not job.supporting_inputs:
        return (), ()
    manifest: SupportingInputManifest = load_supporting_inputs(workspace, job)
    pairs: list[tuple[ArtifactRef, SourceRef]] = []
    for record in manifest.inputs:
        if record.kind.value == "pdf":
            pairs.append(
                (
                    record.original_artifact,
                    SourceRef(
                        kind=SourceRefKind.APPROVED_ARTIFACT,
                        ref_id=record.original_artifact.artifact_id,
                        content_sha256=record.original_artifact.content_sha256,
                    ),
                )
            )
            pairs.extend(
                (
                    page.artifact,
                    SourceRef(
                        kind=SourceRefKind.PDF_PAGE,
                        ref_id=f"{record.input_id}:P{page.page_number:04d}",
                        content_sha256=page.artifact.content_sha256,
                    ),
                )
                for page in record.rendered_pages
            )
        elif record.kind.value == "image":
            pairs.append(
                (
                    record.original_artifact,
                    SourceRef(
                        kind=SourceRefKind.IMAGE,
                        ref_id=record.input_id,
                        content_sha256=record.original_artifact.content_sha256,
                    ),
                )
            )
        else:
            if record.canonical_text_artifact is None:
                raise integrity_error("SUPPORTING_INPUT_MANIFEST_INVALID", "notes text is missing")
            pairs.append(
                (
                    record.canonical_text_artifact,
                    SourceRef(
                        kind=SourceRefKind.NOTES,
                        ref_id=record.input_id,
                        content_sha256=record.canonical_text_artifact.content_sha256,
                    ),
                )
            )
    pairs.sort(key=lambda item: item[0].artifact_id)
    return tuple(item[0] for item in pairs), tuple(item[1] for item in pairs)


def evidence_request_fingerprint(
    *,
    job_id: str,
    transcript_fingerprint: str,
    media_fingerprint: str,
    duration_ms: int | None,
    assignment: EvidenceAssignmentContext,
    contract: AnalyzerContract,
    segments: tuple[TranscriptSegment, ...],
    transcript_refs: tuple[SourceRef, ...],
    frame_records: tuple[FrameRecord, ...],
    frame_refs: tuple[SourceRef, ...],
    approved_artifacts: tuple[ArtifactRef, ...],
    approved_artifact_refs: tuple[SourceRef, ...],
    ambiguities: tuple[AmbiguityRecord, ...],
) -> str:
    return fingerprint(
        {
            "job_id": job_id,
            "transcript_bundle_fingerprint": transcript_fingerprint,
            "media_bundle_fingerprint": media_fingerprint,
            "duration_ms": duration_ms,
            "assignment": assignment.model_dump(mode="json"),
            "analyzer_contract": contract.model_dump(mode="json", exclude_none=True),
            "transcript_segments": [item.model_dump(mode="json") for item in segments],
            "transcript_refs": [item.model_dump(mode="json") for item in transcript_refs],
            "frame_records": [item.model_dump(mode="json") for item in frame_records],
            "frame_refs": [item.model_dump(mode="json") for item in frame_refs],
            "approved_artifacts": [item.model_dump(mode="json") for item in approved_artifacts],
            "approved_artifact_refs": [
                item.model_dump(mode="json") for item in approved_artifact_refs
            ],
            "unresolved_ambiguities": [item.model_dump(mode="json") for item in ambiguities],
            "implementation_version": EVIDENCE_IMPLEMENTATION_VERSION,
        }
    )


def _expected_request(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
    *,
    created_at: datetime,
) -> EvidenceRequestManifest:
    contract = AnalyzerContract(
        atomic_proposition_contract=(
            ATOMIC_PROPOSITION_RULE if requires_transcript_refinement(job) else None
        )
    )
    assignment = EvidenceAssignmentContext(
        task_type=job.assignment.task_type,
        language=job.assignment.language,
        requirements=[
            AssignmentRequirement(requirement_id=f"R{index:06d}", text=text)
            for index, text in enumerate(job.assignment.requirements, start=1)
        ],
    )
    transcript_refs = tuple(_transcript_ref(item) for item in segments)
    frame_records = tuple(
        media_manifest.frame_index.records if media_manifest.frame_index is not None else ()
    )
    frames = tuple(_frame_refs(media_manifest))
    approved_artifacts, approved_artifact_refs = _supporting_catalog(workspace, job)
    request_ambiguities = _request_ambiguities(transcript_manifest, segments)
    ambiguities = tuple(
        [
            *request_ambiguities,
            *_refinement_ambiguities(
                workspace,
                job,
                media_manifest,
                transcript_manifest,
                segments,
                first_id=len(request_ambiguities) + 1,
            ),
        ]
    )
    media_fingerprint = media_bundle_fingerprint(media_manifest)
    request_fingerprint = evidence_request_fingerprint(
        job_id=job.job_id,
        transcript_fingerprint=transcript_manifest.transcription_fingerprint,
        media_fingerprint=media_fingerprint,
        duration_ms=media_manifest.probe.duration_ms if media_manifest.probe is not None else None,
        assignment=assignment,
        contract=contract,
        segments=segments,
        transcript_refs=transcript_refs,
        frame_records=frame_records,
        frame_refs=frames,
        approved_artifacts=approved_artifacts,
        approved_artifact_refs=approved_artifact_refs,
        ambiguities=ambiguities,
    )
    return EvidenceRequestManifest(
        job_id=job.job_id,
        request_fingerprint=request_fingerprint,
        transcript_bundle_fingerprint=transcript_manifest.transcription_fingerprint,
        media_bundle_fingerprint=media_fingerprint,
        duration_ms=media_manifest.probe.duration_ms if media_manifest.probe is not None else None,
        assignment=assignment,
        analyzer_contract=contract,
        transcript_segments=list(segments),
        transcript_refs=list(transcript_refs),
        frame_records=list(frame_records),
        frame_refs=list(frames),
        approved_artifacts=list(approved_artifacts),
        approved_artifact_refs=list(approved_artifact_refs),
        unresolved_ambiguities=list(ambiguities),
        created_at=created_at,
    )


def prepare_evidence_request(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
) -> EvidencePrepareResult:
    if workspace.evidence_request_json.exists():
        stored = _read_bounded_model(
            workspace.evidence_request_json,
            EvidenceRequestManifest,
            maximum_bytes=_MAXIMUM_REQUEST_BYTES,
            code="EVIDENCE_REQUEST_INVALID",
        )
        expected = _expected_request(
            workspace,
            job,
            media_manifest,
            transcript_manifest,
            segments,
            created_at=stored.created_at,
        )
        if stored != expected:
            raise state_error(
                "EVIDENCE_REQUEST_STALE",
                "prepared evidence request no longer matches canonical inputs",
            )
        return EvidencePrepareResult(stored, reused=True)
    request = _expected_request(
        workspace,
        job,
        media_manifest,
        transcript_manifest,
        segments,
        created_at=datetime.now(UTC),
    )
    payload = pretty_json_bytes(request)
    if len(payload) > _MAXIMUM_REQUEST_BYTES:
        raise state_error("EVIDENCE_REQUEST_TOO_LARGE", "evidence request exceeds its limit")
    atomic_write_bytes(workspace.evidence_request_json, payload, private=True)
    stored = _read_bounded_model(
        workspace.evidence_request_json,
        EvidenceRequestManifest,
        maximum_bytes=_MAXIMUM_REQUEST_BYTES,
        code="EVIDENCE_REQUEST_INVALID",
    )
    if stored != request:
        raise integrity_error("EVIDENCE_REQUEST_INVALID", "evidence request write is invalid")
    return EvidencePrepareResult(stored, reused=False)


def load_evidence_request(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
) -> EvidenceRequestManifest:
    stored = _read_bounded_model(
        workspace.evidence_request_json,
        EvidenceRequestManifest,
        maximum_bytes=_MAXIMUM_REQUEST_BYTES,
        code="EVIDENCE_REQUEST_INVALID",
    )
    expected = _expected_request(
        workspace,
        job,
        media_manifest,
        transcript_manifest,
        segments,
        created_at=stored.created_at,
    )
    if stored != expected:
        raise state_error("EVIDENCE_REQUEST_STALE", "evidence request is stale")
    return stored


def _fixture_identity(identity: AnalyzerIdentity) -> bool:
    tokens = {
        token
        for value in (identity.provider, identity.model, identity.executor)
        for token in re.split(r"[^a-z0-9]+", value.casefold())
        if token
    }
    return identity.execution_mode is AnalyzerExecutionMode.TEST_FIXTURE or bool(
        tokens & _FIXTURE_MARKERS
    )


def _validate_response(
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
    *,
    allow_fixture: bool,
) -> None:
    if response.request_fingerprint != request.request_fingerprint:
        raise state_error("EVIDENCE_RESPONSE_STALE", "evidence response targets another request")
    if response.created_at < request.created_at:
        raise integrity_error(
            "EVIDENCE_RESPONSE_INVALID", "evidence response timestamp predates its request"
        )
    if not allow_fixture and (
        response.analyzer.execution_mode is not AnalyzerExecutionMode.EXTERNAL_EXCHANGE
        or _fixture_identity(response.analyzer)
    ):
        raise state_error(
            "FIXTURE_ANALYZER_FORBIDDEN",
            "fixture analyzer identities are not accepted by production import",
        )
    if (
        response.analyzer.execution_mode is AnalyzerExecutionMode.TEST_FIXTURE
        and response.analyzer.external_api_used
    ):
        raise state_error(
            "FIXTURE_ANALYZER_INVALID",
            "fixture analyzers cannot claim external API execution",
        )
    allowed_refs = {
        (item.kind, item.ref_id): item
        for item in [
            *request.transcript_refs,
            *request.frame_refs,
            *request.approved_artifact_refs,
        ]
    }
    segment_by_id = {item.segment_id: item for item in request.transcript_segments}
    for record in response.evidence_records:
        if record.review_status is not ReviewStatus.UNREVIEWED:
            raise state_error(
                "ANALYZER_REVIEW_STATUS_FORBIDDEN",
                "an analyzer cannot assign a reviewer decision",
            )
        if record.interpretation_level is InterpretationLevel.USER_OPINION:
            raise state_error(
                "GENERATED_USER_OPINION_FORBIDDEN",
                "an analyzer cannot generate the user's opinion",
            )
        for source_ref in record.source_refs:
            if allowed_refs.get((source_ref.kind, source_ref.ref_id)) != source_ref:
                raise integrity_error(
                    "EVIDENCE_SOURCE_REF_INVALID",
                    "evidence response contains an unknown or altered source reference",
                )
        if record.interpretation_level is InterpretationLevel.VERBATIM:
            transcript_ids = [
                item.ref_id
                for item in record.source_refs
                if item.kind is SourceRefKind.TRANSCRIPT_SEGMENT
            ]
            if not any(
                record.observation
                in {
                    segment_by_id[item].raw_text,
                    segment_by_id[item].normalized_text,
                }
                for item in transcript_ids
            ):
                raise integrity_error(
                    "VERBATIM_EVIDENCE_INVALID",
                    "verbatim evidence does not exactly match a referenced transcript segment",
                )
    response_ambiguities = {item.ambiguity_id: item for item in response.ambiguity_records}
    for required in request.unresolved_ambiguities:
        returned = response_ambiguities.get(required.ambiguity_id)
        if returned != required:
            raise integrity_error(
                "UNRESOLVED_AMBIGUITY_LOST",
                "an unresolved input ambiguity was omitted or altered",
            )
    for ambiguity in response.ambiguity_records:
        if ambiguity.review_status is not ReviewStatus.NEEDS_REVIEW:
            raise state_error(
                "ANALYZER_REVIEW_STATUS_FORBIDDEN",
                "an analyzer cannot assign an ambiguity reviewer decision",
            )
        if (
            request.duration_ms is not None
            and ambiguity.end_ms is not None
            and ambiguity.end_ms > request.duration_ms
        ):
            raise integrity_error(
                "AMBIGUITY_TIMESTAMP_INVALID", "ambiguity timestamp exceeds source duration"
            )
        for source_ref in ambiguity.source_refs:
            if allowed_refs.get((source_ref.kind, source_ref.ref_id)) != source_ref:
                raise integrity_error(
                    "EVIDENCE_SOURCE_REF_INVALID",
                    "ambiguity contains an unknown or altered source reference",
                )


def read_evidence_response(
    response_path: Path,
    request: EvidenceRequestManifest,
    *,
    allow_fixture: bool,
) -> EvidenceResponseManifest:
    response = _read_bounded_model(
        response_path.absolute(),
        EvidenceResponseManifest,
        maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
        code="EVIDENCE_RESPONSE_INVALID",
    )
    _validate_response(request, response, allow_fixture=allow_fixture)
    return response


def evidence_bundle_fingerprint(
    transcript_manifest: TranscriptManifest,
    media_manifest: MediaManifest,
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
) -> str:
    return _evidence_bundle_fingerprint_with_analyzer_contract(
        transcript_manifest,
        media_manifest,
        request,
        response,
        analyzer_contract=request.analyzer_contract.model_dump(mode="json", exclude_none=True),
    )


def _evidence_bundle_fingerprint_with_analyzer_contract(
    transcript_manifest: TranscriptManifest,
    media_manifest: MediaManifest,
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
    *,
    analyzer_contract: dict[str, object],
) -> str:
    return fingerprint(
        {
            "transcript_bundle_fingerprint": transcript_manifest.transcription_fingerprint,
            "media_bundle_fingerprint": media_bundle_fingerprint(media_manifest),
            "frame_index_artifact": (
                media_manifest.frame_index_artifact.model_dump(mode="json")
                if media_manifest.frame_index_artifact is not None
                else None
            ),
            "request_fingerprint": request.request_fingerprint,
            "analyzer_contract": analyzer_contract,
            "response": response.model_dump(mode="json"),
            "implementation_version": EVIDENCE_IMPLEMENTATION_VERSION,
        }
    )


def _evidence_bundle_fingerprint_matches(
    transcript_manifest: TranscriptManifest,
    media_manifest: MediaManifest,
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
    stored_fingerprint: str,
) -> bool:
    canonical_fingerprint = evidence_bundle_fingerprint(
        transcript_manifest, media_manifest, request, response
    )
    if stored_fingerprint == canonical_fingerprint:
        return True
    if request.analyzer_contract.atomic_proposition_contract is None:
        transitional_fingerprint = _evidence_bundle_fingerprint_with_analyzer_contract(
            transcript_manifest,
            media_manifest,
            request,
            response,
            analyzer_contract=request.analyzer_contract.model_dump(mode="json"),
        )
        return stored_fingerprint == transitional_fingerprint
    return False


def _staging_root(workspace: JobWorkspace) -> Path:
    root = workspace.safe_path("evidence/staging")
    ensure_private_directory(root.parent)
    ensure_private_directory(root)
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)() or root.is_mount():
        raise integrity_error("EVIDENCE_STAGING_INVALID", "evidence staging is invalid")
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
            raise integrity_error("EVIDENCE_STAGING_INVALID", "evidence staging is invalid")
        shutil.rmtree(child)


def create_attempt_directory(workspace: JobWorkspace) -> Path:
    attempt = _staging_root(workspace) / uuid.uuid4().hex
    ensure_private_directory(attempt, exist_ok=False)
    return attempt


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
        raise integrity_error("EVIDENCE_STAGING_INVALID", "evidence staging is invalid")
    shutil.rmtree(attempt)


def _verify_artifact(path: Path, artifact: ArtifactRef) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != artifact.size_bytes
            or sha256_file(path) != artifact.content_sha256
        ):
            raise integrity_error("EVIDENCE_BUNDLE_TAMPERED", "evidence artifact is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "EVIDENCE_BUNDLE_TAMPERED", "evidence artifact is unavailable"
        ) from None


def _publish_artifact(
    workspace: JobWorkspace,
    staged_path: Path,
    *,
    artifact_type: str,
    directory: str,
    filename: str,
) -> ArtifactRef:
    content_sha256 = sha256_file(staged_path)
    artifact_model = (
        EvidenceLedgerArtifactRef if artifact_type == "evidence_records" else ArtifactRef
    )
    artifact = artifact_model(
        artifact_id=f"{artifact_type}-{content_sha256[:16]}",
        artifact_type=artifact_type,
        content_sha256=content_sha256,
        size_bytes=staged_path.stat(follow_symlinks=False).st_size,
        relative_storage_path=f"evidence/{directory}/{content_sha256}/{filename}",
    )
    target = workspace.safe_path(artifact.relative_storage_path)
    ensure_private_directory(target.parent.parent)
    ensure_private_directory(target.parent)
    atomic_copy_exclusive(
        staged_path, target, verifier=lambda item: _verify_artifact(item, artifact)
    )
    _verify_artifact(target, artifact)
    return artifact


def _staged_artifact(
    staged_path: Path,
    *,
    artifact_type: str,
    directory: str,
    filename: str,
) -> ArtifactRef:
    content_sha256 = sha256_file(staged_path)
    artifact_model = (
        EvidenceLedgerArtifactRef if artifact_type == "evidence_records" else ArtifactRef
    )
    return artifact_model(
        artifact_id=f"{artifact_type}-{content_sha256[:16]}",
        artifact_type=artifact_type,
        content_sha256=content_sha256,
        size_bytes=staged_path.stat(follow_symlinks=False).st_size,
        relative_storage_path=f"evidence/{directory}/{content_sha256}/{filename}",
    )


def _read_evidence_records(
    path: Path, artifact: EvidenceLedgerArtifactRef
) -> tuple[EvidenceRecord, ...]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise integrity_error("EVIDENCE_LEDGER_INVALID", "evidence ledger is unavailable") from None
    if artifact.size_bytes > _MAXIMUM_LEDGER_BYTES or metadata.st_size > _MAXIMUM_LEDGER_BYTES:
        raise integrity_error("EVIDENCE_LEDGER_INVALID", "evidence ledger exceeds its limit")
    _verify_artifact(path, artifact)
    payload = path.read_bytes()
    if not payload:
        return ()
    if not payload.endswith(b"\n"):
        raise integrity_error("EVIDENCE_LEDGER_INVALID", "evidence ledger is incomplete")
    try:
        records = tuple(
            EvidenceRecord.model_validate_json(line, strict=True) for line in payload.splitlines()
        )
    except ValidationError:
        raise integrity_error("EVIDENCE_LEDGER_INVALID", "evidence ledger is malformed") from None
    if [item.evidence_id for item in records] != [
        f"E{index:06d}" for index in range(1, len(records) + 1)
    ]:
        raise integrity_error("EVIDENCE_LEDGER_INVALID", "evidence ledger IDs are inconsistent")
    return records


def _stage_manifest(
    manifest: EvidenceManifest,
    transcript: TranscriptManifest,
    media: MediaManifest,
    request: EvidenceRequestManifest,
) -> StageManifest:
    return StageManifest(
        job_id=manifest.job_id,
        stage="evidence",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.EVIDENCE_READY,
        input_artifacts=[
            transcript.segments_artifact,
            *([media.frame_index_artifact] if media.frame_index_artifact is not None else []),
            *request.approved_artifacts,
        ],
        output_artifacts=[
            manifest.response_artifact,
            manifest.evidence_artifact,
            manifest.ambiguity_artifact,
        ],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-evidence-import",
            version=__version__,
        ),
        configuration_sha256=manifest.evidence_bundle_fingerprint,
        started_at=manifest.started_at,
        finished_at=manifest.created_at,
    )


def stage_evidence(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
    attempt: Path,
) -> EvidenceImportResult:
    _validate_response(request, response, allow_fixture=True)
    evidence_records = tuple(response.evidence_records)
    ambiguity_records = tuple(response.ambiguity_records)
    from video_evidence_pipeline.search import verify_index_rebuildable

    started_at = datetime.now(UTC)
    evidence_payload = b"".join(canonical_json_bytes(item) + b"\n" for item in evidence_records)
    ambiguity_ledger = AmbiguityLedger(records=list(ambiguity_records))
    ambiguity_payload = pretty_json_bytes(ambiguity_ledger)
    response_payload = pretty_json_bytes(response)
    if (
        len(evidence_payload) > _MAXIMUM_LEDGER_BYTES
        or len(ambiguity_payload) > _MAXIMUM_LEDGER_BYTES
        or len(response_payload) > _MAXIMUM_RESPONSE_BYTES
    ):
        raise state_error("EVIDENCE_LEDGER_TOO_LARGE", "evidence output exceeds its limit")
    evidence_path = attempt / "evidence.jsonl"
    ambiguity_path = attempt / "ambiguities.json"
    response_path = attempt / "validated-response.json"
    atomic_write_bytes(evidence_path, evidence_payload, private=True)
    atomic_write_bytes(ambiguity_path, ambiguity_payload, private=True)
    atomic_write_bytes(response_path, response_payload, private=True)
    staged_evidence = _staged_artifact(
        evidence_path,
        artifact_type="evidence_records",
        directory="records",
        filename="evidence.jsonl",
    )
    staged_ambiguity = _staged_artifact(
        ambiguity_path,
        artifact_type="ambiguity_ledger",
        directory="ambiguities",
        filename="ambiguities.json",
    )
    staged_response = _staged_artifact(
        response_path,
        artifact_type="validated_evidence_response",
        directory="responses",
        filename="response.json",
    )
    _verify_artifact(ambiguity_path, staged_ambiguity)
    _verify_artifact(response_path, staged_response)
    if _read_evidence_records(evidence_path, staged_evidence) != evidence_records:
        raise integrity_error("EVIDENCE_LEDGER_INVALID", "staged evidence ledger changed")
    stored_ambiguities = _read_bounded_model(
        ambiguity_path,
        AmbiguityLedger,
        maximum_bytes=_MAXIMUM_LEDGER_BYTES,
        code="AMBIGUITY_LEDGER_INVALID",
    )
    if tuple(stored_ambiguities.records) != ambiguity_records:
        raise integrity_error("AMBIGUITY_LEDGER_INVALID", "staged ambiguity ledger changed")
    stored_response = _read_bounded_model(
        response_path,
        EvidenceResponseManifest,
        maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
        code="EVIDENCE_RESPONSE_INVALID",
    )
    if stored_response != response:
        raise integrity_error("EVIDENCE_RESPONSE_INVALID", "validated response changed")
    created_at = datetime.now(UTC)
    response_fingerprint = fingerprint(response)
    bundle_fingerprint = evidence_bundle_fingerprint(
        transcript_manifest, media_manifest, request, response
    )
    manifest = EvidenceManifest(
        job_id=job.job_id,
        evidence_bundle_fingerprint=bundle_fingerprint,
        transcript_bundle_fingerprint=transcript_manifest.transcription_fingerprint,
        media_bundle_fingerprint=media_bundle_fingerprint(media_manifest),
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        response_created_at=response.created_at,
        implementation_version=EVIDENCE_IMPLEMENTATION_VERSION,
        analyzer=response.analyzer,
        response_artifact=staged_response,
        evidence_artifact=staged_evidence,
        ambiguity_artifact=staged_ambiguity,
        evidence_count=len(evidence_records),
        ambiguity_count=len(ambiguity_records),
        started_at=started_at,
        created_at=created_at,
    )
    verify_index_rebuildable(
        job.job_id,
        transcript_manifest,
        segments,
        manifest,
        evidence_records,
    )
    stage = _stage_manifest(manifest, transcript_manifest, media_manifest, request)
    return EvidenceImportResult(
        manifest,
        stage,
        evidence_records,
        ambiguity_records,
        reused=False,
    )


def _validate_staged_evidence_binding(
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
    staged: EvidenceImportResult,
) -> None:
    manifest = staged.evidence_manifest
    if (
        manifest.job_id != job.job_id
        or manifest.transcript_bundle_fingerprint != transcript_manifest.transcription_fingerprint
        or manifest.media_bundle_fingerprint != media_bundle_fingerprint(media_manifest)
        or manifest.request_fingerprint != request.request_fingerprint
        or manifest.response_fingerprint != fingerprint(response)
        or manifest.response_created_at != response.created_at
        or manifest.analyzer != response.analyzer
        or manifest.evidence_bundle_fingerprint
        != evidence_bundle_fingerprint(transcript_manifest, media_manifest, request, response)
        or staged.evidence_records != tuple(response.evidence_records)
        or staged.ambiguity_records != tuple(response.ambiguity_records)
        or staged.stage_manifest
        != _stage_manifest(manifest, transcript_manifest, media_manifest, request)
    ):
        raise integrity_error(
            "EVIDENCE_SNAPSHOT_STALE", "staged evidence no longer matches its input snapshot"
        )


def commit_staged_evidence(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
    staged: EvidenceImportResult,
    *,
    attempt: Path | None = None,
) -> EvidenceImportResult:
    _validate_staged_evidence_binding(
        job, media_manifest, transcript_manifest, request, response, staged
    )
    manifest = staged.evidence_manifest
    stage = staged.stage_manifest
    if attempt is not None:
        root = _staging_root(workspace)
        if (
            attempt.parent != root
            or _ATTEMPT_NAME.fullmatch(attempt.name) is None
            or attempt.is_symlink()
            or getattr(attempt, "is_junction", lambda: False)()
            or attempt.is_mount()
            or not attempt.is_dir()
        ):
            raise integrity_error("EVIDENCE_STAGING_INVALID", "evidence staging is invalid")
        published_response = _publish_artifact(
            workspace,
            attempt / "validated-response.json",
            artifact_type="validated_evidence_response",
            directory="responses",
            filename="response.json",
        )
        published_evidence = _publish_artifact(
            workspace,
            attempt / "evidence.jsonl",
            artifact_type="evidence_records",
            directory="records",
            filename="evidence.jsonl",
        )
        published_ambiguity = _publish_artifact(
            workspace,
            attempt / "ambiguities.json",
            artifact_type="ambiguity_ledger",
            directory="ambiguities",
            filename="ambiguities.json",
        )
        if (
            published_response != manifest.response_artifact
            or published_evidence != manifest.evidence_artifact
            or published_ambiguity != manifest.ambiguity_artifact
        ):
            raise integrity_error("EVIDENCE_BUNDLE_TAMPERED", "published evidence changed")
    else:
        _verify_artifact(
            workspace.safe_path(manifest.response_artifact.relative_storage_path),
            manifest.response_artifact,
        )
        _verify_artifact(
            workspace.safe_path(manifest.evidence_artifact.relative_storage_path),
            manifest.evidence_artifact,
        )
        _verify_artifact(
            workspace.safe_path(manifest.ambiguity_artifact.relative_storage_path),
            manifest.ambiguity_artifact,
        )
    if workspace.evidence_manifest_json.exists():
        primary = _read_bounded_model(
            workspace.evidence_manifest_json,
            EvidenceManifest,
            maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
            code="EVIDENCE_MANIFEST_INVALID",
        )
        if primary != manifest:
            raise integrity_error(
                "EVIDENCE_FINGERPRINT_MISMATCH",
                "another evidence bundle uses a different response",
            )
        if workspace.evidence_stage_manifest_json.exists():
            stored_stage = _read_bounded_model(
                workspace.evidence_stage_manifest_json,
                StageManifest,
                maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
                code="STAGE_MANIFEST_INVALID",
            )
            if stored_stage != stage:
                raise integrity_error(
                    "STAGE_MANIFEST_MISMATCH", "evidence stage manifest is invalid"
                )
        else:
            atomic_write_json(workspace.evidence_stage_manifest_json, stage, private=True)
        return staged
    if workspace.evidence_candidate_manifest_json.exists():
        candidate = _read_bounded_model(
            workspace.evidence_candidate_manifest_json,
            EvidenceManifest,
            maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
            code="EVIDENCE_MANIFEST_INVALID",
        )
        if candidate != manifest:
            raise integrity_error("EVIDENCE_MANIFEST_MISMATCH", "evidence candidate changed")
    else:
        atomic_write_json(workspace.evidence_candidate_manifest_json, manifest, private=True)
    candidate = _read_bounded_model(
        workspace.evidence_candidate_manifest_json,
        EvidenceManifest,
        maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
        code="EVIDENCE_MANIFEST_INVALID",
    )
    if candidate != manifest:
        raise integrity_error("EVIDENCE_MANIFEST_MISMATCH", "evidence candidate changed")
    if workspace.evidence_stage_manifest_json.exists():
        stored_stage = _read_bounded_model(
            workspace.evidence_stage_manifest_json,
            StageManifest,
            maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
            code="STAGE_MANIFEST_INVALID",
        )
        if stored_stage != stage:
            raise integrity_error("STAGE_MANIFEST_MISMATCH", "evidence stage manifest is invalid")
    else:
        atomic_write_json(workspace.evidence_stage_manifest_json, stage, private=True)
    # The primary evidence manifest is the canonical commit marker and is written last.
    atomic_write_json(workspace.evidence_manifest_json, manifest, private=True)
    return staged


def publish_evidence(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
    attempt: Path,
) -> EvidenceImportResult:
    if workspace.evidence_manifest_json.exists():
        existing = load_evidence_result(
            workspace,
            job,
            media_manifest,
            transcript_manifest,
            segments,
        )
        if existing.evidence_manifest.response_fingerprint != fingerprint(response):
            raise integrity_error(
                "EVIDENCE_FINGERPRINT_MISMATCH",
                "another evidence bundle uses a different response",
            )
        return existing
    staged = stage_evidence(
        workspace,
        job,
        media_manifest,
        transcript_manifest,
        segments,
        request,
        response,
        attempt,
    )
    return commit_staged_evidence(
        workspace,
        job,
        media_manifest,
        transcript_manifest,
        segments,
        request,
        response,
        staged,
        attempt=attempt,
    )


def _verify_evidence_manifest(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
    manifest: EvidenceManifest,
) -> EvidenceImportResult:
    request = load_evidence_request(workspace, job, media_manifest, transcript_manifest, segments)
    response_path = workspace.safe_path(manifest.response_artifact.relative_storage_path)
    evidence_path = workspace.safe_path(manifest.evidence_artifact.relative_storage_path)
    ambiguity_path = workspace.safe_path(manifest.ambiguity_artifact.relative_storage_path)
    evidence_records = _read_evidence_records(evidence_path, manifest.evidence_artifact)
    ambiguity_ledger = _read_bounded_model(
        ambiguity_path,
        AmbiguityLedger,
        maximum_bytes=_MAXIMUM_LEDGER_BYTES,
        code="AMBIGUITY_LEDGER_INVALID",
    )
    _verify_artifact(ambiguity_path, manifest.ambiguity_artifact)
    ambiguity_records = tuple(ambiguity_ledger.records)
    if manifest.response_artifact.size_bytes > _MAXIMUM_RESPONSE_BYTES:
        raise integrity_error(
            "EVIDENCE_RESPONSE_INVALID", "validated response exceeds the maximum size"
        )
    response = _read_bounded_model(
        response_path,
        EvidenceResponseManifest,
        maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
        code="EVIDENCE_RESPONSE_INVALID",
    )
    _verify_artifact(response_path, manifest.response_artifact)
    if (
        response.request_fingerprint != manifest.request_fingerprint
        or response.analyzer != manifest.analyzer
        or response.created_at != manifest.response_created_at
        or tuple(response.evidence_records) != evidence_records
        or tuple(response.ambiguity_records) != ambiguity_records
    ):
        raise integrity_error(
            "EVIDENCE_MANIFEST_MISMATCH", "validated response and ledgers disagree"
        )
    _validate_response(request, response, allow_fixture=True)
    if (
        manifest.job_id != job.job_id
        or manifest.implementation_version != EVIDENCE_IMPLEMENTATION_VERSION
        or manifest.transcript_bundle_fingerprint != transcript_manifest.transcription_fingerprint
        or manifest.media_bundle_fingerprint != media_bundle_fingerprint(media_manifest)
        or manifest.request_fingerprint != request.request_fingerprint
        or manifest.response_fingerprint != fingerprint(response)
        or not _evidence_bundle_fingerprint_matches(
            transcript_manifest,
            media_manifest,
            request,
            response,
            manifest.evidence_bundle_fingerprint,
        )
        or manifest.evidence_count != len(evidence_records)
        or manifest.ambiguity_count != len(ambiguity_records)
        or not manifest.response_artifact.relative_storage_path.startswith("evidence/responses/")
        or manifest.evidence_artifact.relative_storage_path
        != (f"evidence/records/{manifest.evidence_artifact.content_sha256}/evidence.jsonl")
        or not manifest.ambiguity_artifact.relative_storage_path.startswith("evidence/ambiguities/")
    ):
        raise integrity_error("EVIDENCE_MANIFEST_MISMATCH", "evidence bundle is inconsistent")
    from video_evidence_pipeline.search import verify_index_rebuildable

    verify_index_rebuildable(
        job.job_id,
        transcript_manifest,
        segments,
        manifest,
        evidence_records,
    )
    stage = _stage_manifest(manifest, transcript_manifest, media_manifest, request)
    return EvidenceImportResult(manifest, stage, evidence_records, ambiguity_records, reused=True)


def load_evidence_result(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
) -> EvidenceImportResult:
    manifest = _read_bounded_model(
        workspace.evidence_manifest_json,
        EvidenceManifest,
        maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
        code="EVIDENCE_MANIFEST_INVALID",
    )
    result = _verify_evidence_manifest(
        workspace, job, media_manifest, transcript_manifest, segments, manifest
    )
    stage = _read_bounded_model(
        workspace.evidence_stage_manifest_json,
        StageManifest,
        maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
        code="STAGE_MANIFEST_INVALID",
    )
    if stage != result.stage_manifest:
        raise integrity_error("STAGE_MANIFEST_MISMATCH", "evidence stage manifest is invalid")
    if workspace.evidence_candidate_manifest_json.exists():
        candidate = _read_bounded_model(
            workspace.evidence_candidate_manifest_json,
            EvidenceManifest,
            maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
            code="EVIDENCE_MANIFEST_INVALID",
        )
        if candidate != manifest:
            raise integrity_error(
                "EVIDENCE_MANIFEST_MISMATCH", "evidence candidate manifest is inconsistent"
            )
    return result


def recover_evidence_result(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    transcript_manifest: TranscriptManifest,
    segments: tuple[TranscriptSegment, ...],
    *,
    commit: bool = True,
) -> EvidenceImportResult | None:
    primary_exists = workspace.evidence_manifest_json.exists()
    candidate_exists = workspace.evidence_candidate_manifest_json.exists()
    stage_exists = workspace.evidence_stage_manifest_json.exists()
    if not primary_exists and not candidate_exists:
        if stage_exists:
            raise integrity_error(
                "EVIDENCE_MANIFEST_INCOMPLETE",
                "evidence stage cannot be recovered without its candidate manifest",
            )
        return None
    if primary_exists:
        manifest = _read_bounded_model(
            workspace.evidence_manifest_json,
            EvidenceManifest,
            maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
            code="EVIDENCE_MANIFEST_INVALID",
        )
    else:
        manifest = _read_bounded_model(
            workspace.evidence_candidate_manifest_json,
            EvidenceManifest,
            maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
            code="EVIDENCE_MANIFEST_INVALID",
        )
    result = _verify_evidence_manifest(
        workspace, job, media_manifest, transcript_manifest, segments, manifest
    )
    if not commit:
        return result
    if not stage_exists:
        atomic_write_json(
            workspace.evidence_stage_manifest_json, result.stage_manifest, private=True
        )
    else:
        stage = _read_bounded_model(
            workspace.evidence_stage_manifest_json,
            StageManifest,
            maximum_bytes=_MAXIMUM_RESPONSE_BYTES,
            code="STAGE_MANIFEST_INVALID",
        )
        if stage != result.stage_manifest:
            raise integrity_error("STAGE_MANIFEST_MISMATCH", "evidence stage manifest is invalid")
    if not primary_exists:
        atomic_write_json(workspace.evidence_manifest_json, manifest, private=True)
    return load_evidence_result(workspace, job, media_manifest, transcript_manifest, segments)
