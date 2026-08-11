"""Explicit, immutable transcript-refinement exchange."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.evidence import SourceRef, SourceRefKind
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import JobSpec
from video_evidence_pipeline.domain.media import MediaManifest
from video_evidence_pipeline.domain.refinement import (
    RefinementAmbiguity,
    RefinementAmbiguityLedger,
    RefinementEdit,
    RefinementEditKind,
    RefinementEditLedger,
    RefinementExecutionMode,
    RefinementManifest,
    RefinementRequestManifest,
    RefinementResponseManifest,
)
from video_evidence_pipeline.domain.serialization import (
    canonical_json_bytes,
    fingerprint,
    pretty_json_bytes,
)
from video_evidence_pipeline.domain.supporting import SupportingInputManifest
from video_evidence_pipeline.domain.transcription import TranscriptManifest, TranscriptSegment
from video_evidence_pipeline.errors import VepError, integrity_error, state_error
from video_evidence_pipeline.storage.atomic import atomic_write_bytes, atomic_write_new_bytes
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    enforce_private_file,
    ensure_private_directory,
)
from video_evidence_pipeline.supporting import load_supporting_inputs, prepare_supporting_inputs
from video_evidence_pipeline.transcription.pipeline import normalize_transcript_text

REFINEMENT_IMPLEMENTATION_VERSION = "transcript-refinement-v1"
_MAXIMUM_ARTIFACT_BYTES = 128 * 1024 * 1024
_FIXTURE_MARKERS = {"fixture", "pytest", "test"}


@dataclass(frozen=True, slots=True)
class RefinementPrepareResult:
    request: RefinementRequestManifest
    reused: bool


@dataclass(frozen=True, slots=True)
class RefinementResult:
    refinement_manifest: RefinementManifest
    segments: tuple[TranscriptSegment, ...]
    edits: tuple[RefinementEdit, ...]
    ambiguities: tuple[RefinementAmbiguity, ...]
    reused: bool


def _read_model[ModelT: BaseModel](path: Path, model: type[ModelT], *, code: str) -> ModelT:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAXIMUM_ARTIFACT_BYTES
        ):
            raise integrity_error(code, "refinement artifact is invalid")
        payload = path.read_bytes()
    except OSError:
        raise integrity_error(code, "refinement artifact is unavailable") from None
    if len(payload) != metadata.st_size:
        raise integrity_error(code, "refinement artifact changed while reading")
    try:
        return model.model_validate_json(payload, strict=True)
    except (UnicodeDecodeError, ValidationError):
        raise integrity_error(code, "refinement artifact is malformed") from None


def _verify_artifact(path: Path, artifact: ArtifactRef) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != artifact.size_bytes
            or sha256_file(path) != artifact.content_sha256
        ):
            raise integrity_error("REFINEMENT_BUNDLE_TAMPERED", "refinement artifact is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "REFINEMENT_BUNDLE_TAMPERED", "refinement artifact is unavailable"
        ) from None


def _store_artifact(
    workspace: JobWorkspace,
    payload: bytes,
    *,
    artifact_type: str,
    directory: str,
    filename: str,
) -> ArtifactRef:
    if not payload or len(payload) > _MAXIMUM_ARTIFACT_BYTES:
        raise state_error("REFINEMENT_OUTPUT_TOO_LARGE", "refinement output exceeds its limit")
    content_sha256 = sha256_bytes(payload)
    artifact = ArtifactRef(
        artifact_id=f"{artifact_type}-{content_sha256[:16]}",
        artifact_type=artifact_type,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        relative_storage_path=f"refinement/{directory}/{content_sha256}/{filename}",
    )
    target = workspace.safe_path(artifact.relative_storage_path)
    for directory_path in (
        workspace.safe_path("refinement"),
        workspace.safe_path(f"refinement/{directory}"),
        target.parent,
    ):
        ensure_private_directory(directory_path)
    try:
        atomic_write_new_bytes(target, payload, private=True)
    except VepError as exc:
        if exc.code != "FILE_EXISTS":
            raise
    _verify_artifact(target, artifact)
    return artifact


def _transcript_ref(segment: TranscriptSegment) -> SourceRef:
    return SourceRef(
        kind=SourceRefKind.TRANSCRIPT_SEGMENT,
        ref_id=segment.segment_id,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        content_sha256=fingerprint(segment),
    )


def _source_catalog(
    media_manifest: MediaManifest,
    raw_segments: tuple[TranscriptSegment, ...],
    supporting: SupportingInputManifest | None,
) -> tuple[tuple[ArtifactRef, ...], tuple[SourceRef, ...]]:
    artifacts: list[ArtifactRef] = []
    refs = [_transcript_ref(segment) for segment in raw_segments]
    if media_manifest.frame_index is not None:
        refs.extend(
            SourceRef(
                kind=SourceRefKind.FRAME,
                ref_id=record.frame_id,
                start_ms=record.timestamp_ms,
                content_sha256=record.artifact.content_sha256,
            )
            for record in media_manifest.frame_index.records
        )
    if supporting is not None:
        for record in supporting.inputs:
            artifacts.append(record.original_artifact)
            if record.kind.value == "pdf":
                if record.canonical_text_artifact is not None:
                    artifacts.append(record.canonical_text_artifact)
                    refs.append(
                        SourceRef(
                            kind=SourceRefKind.PDF_TEXT,
                            ref_id=record.input_id,
                            content_sha256=record.canonical_text_artifact.content_sha256,
                        )
                    )
                for page in record.rendered_pages:
                    artifacts.append(page.artifact)
                    refs.append(
                        SourceRef(
                            kind=SourceRefKind.PDF_PAGE,
                            ref_id=f"{record.input_id}:P{page.page_number:04d}",
                            content_sha256=page.artifact.content_sha256,
                        )
                    )
            elif record.kind.value == "image":
                refs.append(
                    SourceRef(
                        kind=SourceRefKind.IMAGE,
                        ref_id=record.input_id,
                        content_sha256=record.original_artifact.content_sha256,
                    )
                )
            else:
                if record.canonical_text_artifact is None:
                    raise integrity_error(
                        "SUPPORTING_INPUT_MANIFEST_INVALID", "notes text is missing"
                    )
                artifacts.append(record.canonical_text_artifact)
                refs.append(
                    SourceRef(
                        kind=SourceRefKind.NOTES,
                        ref_id=record.input_id,
                        content_sha256=record.canonical_text_artifact.content_sha256,
                    )
                )
    return (
        tuple(
            sorted(
                {item.artifact_id: item for item in artifacts}.values(),
                key=lambda item: item.artifact_id,
            )
        ),
        tuple(sorted(refs, key=lambda item: (item.kind.value, item.ref_id))),
    )


def refinement_request_fingerprint(
    *,
    job: JobSpec,
    raw_manifest: TranscriptManifest,
    raw_segments: tuple[TranscriptSegment, ...],
    support_artifacts: tuple[ArtifactRef, ...],
    source_refs: tuple[SourceRef, ...],
) -> str:
    return fingerprint(
        {
            "job_id": job.job_id,
            "raw_transcript_fingerprint": raw_manifest.transcription_fingerprint,
            "raw_segments_artifact": raw_manifest.segments_artifact.model_dump(mode="json"),
            "transcript_segments": [item.model_dump(mode="json") for item in raw_segments],
            "available_support_artifacts": [
                item.model_dump(mode="json") for item in support_artifacts
            ],
            "available_source_refs": [item.model_dump(mode="json") for item in source_refs],
            "semantic_correction_contract": (
                "Each semantic edit must refine exactly one transcript segment and cite a "
                "non-transcript supporting source; otherwise return one ambiguity and do not "
                "merge propositions."
            ),
            "implementation_version": REFINEMENT_IMPLEMENTATION_VERSION,
        }
    )


def _expected_request(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    raw_manifest: TranscriptManifest,
    raw_segments: tuple[TranscriptSegment, ...],
    *,
    created_at: datetime,
) -> RefinementRequestManifest:
    supporting = load_supporting_inputs(workspace, job) if job.supporting_inputs else None
    support_artifacts, source_refs = _source_catalog(media_manifest, raw_segments, supporting)
    return RefinementRequestManifest(
        job_id=job.job_id,
        request_fingerprint=refinement_request_fingerprint(
            job=job,
            raw_manifest=raw_manifest,
            raw_segments=raw_segments,
            support_artifacts=support_artifacts,
            source_refs=source_refs,
        ),
        raw_transcript_fingerprint=raw_manifest.transcription_fingerprint,
        raw_segments_artifact=raw_manifest.segments_artifact,
        transcript_segments=list(raw_segments),
        available_support_artifacts=list(support_artifacts),
        available_source_refs=list(source_refs),
        created_at=created_at,
    )


def prepare_refinement_request(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    raw_manifest: TranscriptManifest,
    raw_segments: tuple[TranscriptSegment, ...],
) -> RefinementPrepareResult:
    if job.supporting_inputs:
        prepare_supporting_inputs(workspace, job)
    if workspace.refinement_request_json.exists():
        stored = _read_model(
            workspace.refinement_request_json,
            RefinementRequestManifest,
            code="REFINEMENT_REQUEST_INVALID",
        )
        expected = _expected_request(
            workspace,
            job,
            media_manifest,
            raw_manifest,
            raw_segments,
            created_at=stored.created_at,
        )
        if stored != expected:
            raise state_error("REFINEMENT_REQUEST_STALE", "refinement request is stale")
        return RefinementPrepareResult(stored, reused=True)
    request = _expected_request(
        workspace,
        job,
        media_manifest,
        raw_manifest,
        raw_segments,
        created_at=datetime.now(UTC),
    )
    atomic_write_bytes(workspace.refinement_request_json, pretty_json_bytes(request), private=True)
    return RefinementPrepareResult(request, reused=False)


def load_refinement_request(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest,
    raw_manifest: TranscriptManifest,
    raw_segments: tuple[TranscriptSegment, ...],
) -> RefinementRequestManifest:
    return prepare_refinement_request(
        workspace, job, media_manifest, raw_manifest, raw_segments
    ).request


def _fixture_identity(response: RefinementResponseManifest) -> bool:
    tokens = {
        token
        for value in (
            response.refiner.provider,
            response.refiner.model,
            response.refiner.executor,
        )
        for token in re.split(r"[^a-z0-9]+", value.casefold())
        if token
    }
    return response.refiner.execution_mode is RefinementExecutionMode.TEST_FIXTURE or bool(
        tokens & _FIXTURE_MARKERS
    )


def _validate_response(
    request: RefinementRequestManifest,
    response: RefinementResponseManifest,
    *,
    allow_fixture: bool,
) -> None:
    if response.request_fingerprint != request.request_fingerprint:
        raise state_error(
            "REFINEMENT_RESPONSE_STALE", "refinement response targets another request"
        )
    if response.created_at < request.created_at:
        raise integrity_error(
            "REFINEMENT_RESPONSE_INVALID", "refinement response predates its request"
        )
    if not allow_fixture and (
        response.refiner.execution_mode is not RefinementExecutionMode.EXTERNAL_EXCHANGE
        or _fixture_identity(response)
    ):
        raise state_error(
            "FIXTURE_REFINER_FORBIDDEN",
            "fixture refiner identity is not accepted by production import",
        )
    if (
        response.refiner.execution_mode is RefinementExecutionMode.TEST_FIXTURE
        and response.refiner.external_api_used
    ):
        raise state_error(
            "FIXTURE_REFINER_INVALID", "fixture refiner cannot claim external API use"
        )
    known_refs = {(item.kind, item.ref_id): item for item in request.available_source_refs}
    known_segments = {item.segment_id: item for item in request.transcript_segments}
    for edit in response.semantic_edits:
        if edit.kind is not RefinementEditKind.SEMANTIC:
            raise integrity_error(
                "REFINEMENT_RESPONSE_INVALID", "external response may only contain semantic edits"
            )
        if (
            edit.segment_id not in known_segments
            or edit.original_segment_fingerprint != fingerprint(known_segments[edit.segment_id])
        ):
            raise integrity_error(
                "REFINEMENT_SOURCE_INVALID", "semantic edit targets an altered segment"
            )
        for source_ref in edit.source_refs:
            if known_refs.get((source_ref.kind, source_ref.ref_id)) != source_ref:
                raise integrity_error(
                    "REFINEMENT_SOURCE_INVALID", "semantic edit uses an unknown source reference"
                )
    for ambiguity in response.ambiguities:
        if ambiguity.segment_id not in known_segments:
            raise integrity_error(
                "REFINEMENT_SOURCE_INVALID", "refinement ambiguity targets unknown text"
            )
        for source_ref in ambiguity.source_refs:
            if known_refs.get((source_ref.kind, source_ref.ref_id)) != source_ref:
                raise integrity_error(
                    "REFINEMENT_SOURCE_INVALID",
                    "refinement ambiguity uses an unknown source reference",
                )


def read_refinement_response(
    path: Path,
    request: RefinementRequestManifest,
    *,
    allow_fixture: bool,
) -> RefinementResponseManifest:
    response = _read_model(
        path.absolute(), RefinementResponseManifest, code="REFINEMENT_RESPONSE_INVALID"
    )
    _validate_response(request, response, allow_fixture=allow_fixture)
    return response


def automatic_refinement(value: str) -> str:
    """Apply bounded mechanical cleanup only; semantic corrections remain explicit."""

    value = normalize_transcript_text(value)
    value = re.sub(r"([!?.,;:])\1+", r"\1", value)
    return re.sub(r"\b(um|uh|erm)\s+\1\b", r"\1", value, flags=re.IGNORECASE)


def _refined_output(
    raw_segments: tuple[TranscriptSegment, ...],
    response: RefinementResponseManifest,
) -> tuple[tuple[TranscriptSegment, ...], RefinementEditLedger, RefinementAmbiguityLedger]:
    semantic_by_segment = {item.segment_id: item for item in response.semantic_edits}
    edits: list[RefinementEdit] = []
    segments: list[TranscriptSegment] = []
    for segment in raw_segments:
        semantic = semantic_by_segment.get(segment.segment_id)
        refined_text = (
            normalize_transcript_text(semantic.refined_text)
            if semantic is not None
            else automatic_refinement(segment.normalized_text)
        )
        if not refined_text:
            raise integrity_error(
                "REFINEMENT_RESPONSE_INVALID", "refinement cannot erase a segment"
            )
        segments.append(segment.model_copy(update={"normalized_text": refined_text}))
        if semantic is not None:
            edits.append(semantic)
        elif refined_text != segment.normalized_text:
            edits.append(
                RefinementEdit(
                    edit_id="R000000",
                    segment_id=segment.segment_id,
                    original_segment_fingerprint=fingerprint(segment),
                    refined_text=refined_text,
                    kind=RefinementEditKind.AUTOMATIC,
                    source_refs=[_transcript_ref(segment)],
                )
            )
    edits.sort(key=lambda item: item.segment_id)
    return (
        tuple(segments),
        RefinementEditLedger(
            records=[
                item.model_copy(update={"edit_id": f"R{index:06d}"})
                for index, item in enumerate(edits, start=1)
            ]
        ),
        RefinementAmbiguityLedger(records=list(response.ambiguities)),
    )


def refined_transcript_fingerprint(
    raw_manifest: TranscriptManifest,
    refined_segments_artifact: ArtifactRef,
    edit_ledger_artifact: ArtifactRef,
    ambiguity_artifact: ArtifactRef,
    request: RefinementRequestManifest,
    response: RefinementResponseManifest,
) -> str:
    return fingerprint(
        {
            "raw_transcript_fingerprint": raw_manifest.transcription_fingerprint,
            "refined_segments_artifact": refined_segments_artifact.model_dump(mode="json"),
            "edit_ledger_artifact": edit_ledger_artifact.model_dump(mode="json"),
            "ambiguity_artifact": ambiguity_artifact.model_dump(mode="json"),
            "request_fingerprint": request.request_fingerprint,
            "response_fingerprint": fingerprint(response),
            "implementation_version": REFINEMENT_IMPLEMENTATION_VERSION,
        }
    )


def publish_refinement(
    workspace: JobWorkspace,
    job: JobSpec,
    raw_manifest: TranscriptManifest,
    raw_segments: tuple[TranscriptSegment, ...],
    request: RefinementRequestManifest,
    response: RefinementResponseManifest,
) -> RefinementResult:
    _validate_response(request, response, allow_fixture=True)
    if workspace.refinement_manifest_json.exists():
        existing = load_refinement_result(
            workspace, job, None, raw_manifest, raw_segments, request=request
        )
        if existing.refinement_manifest.response_fingerprint != fingerprint(response):
            raise integrity_error(
                "REFINEMENT_FINGERPRINT_MISMATCH", "another refinement bundle already exists"
            )
        return existing
    segments, ledger, ambiguities = _refined_output(raw_segments, response)
    refined_artifact = _store_artifact(
        workspace,
        b"".join(canonical_json_bytes(item) + b"\n" for item in segments),
        artifact_type="refined_transcript_segments",
        directory="segments",
        filename="transcript.jsonl",
    )
    edit_artifact = _store_artifact(
        workspace,
        pretty_json_bytes(ledger),
        artifact_type="refinement_edit_ledger",
        directory="edits",
        filename="edits.json",
    )
    ambiguity_artifact = _store_artifact(
        workspace,
        pretty_json_bytes(ambiguities),
        artifact_type="refinement_ambiguity_ledger",
        directory="ambiguities",
        filename="ambiguities.json",
    )
    response_artifact = _store_artifact(
        workspace,
        pretty_json_bytes(response),
        artifact_type="refinement_response",
        directory="responses",
        filename="validated-response.json",
    )
    started_at = datetime.now(UTC)
    manifest = RefinementManifest(
        job_id=job.job_id,
        raw_transcript_fingerprint=raw_manifest.transcription_fingerprint,
        refined_transcript_fingerprint=refined_transcript_fingerprint(
            raw_manifest,
            refined_artifact,
            edit_artifact,
            ambiguity_artifact,
            request,
            response,
        ),
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=fingerprint(response),
        implementation_version=REFINEMENT_IMPLEMENTATION_VERSION,
        raw_segments_artifact=raw_manifest.segments_artifact,
        refined_segments_artifact=refined_artifact,
        edit_ledger_artifact=edit_artifact,
        ambiguity_artifact=ambiguity_artifact,
        response_artifact=response_artifact,
        edit_count=len(ledger.records),
        ambiguity_count=len(ambiguities.records),
        started_at=started_at,
        created_at=datetime.now(UTC),
    )
    atomic_write_new_bytes(
        workspace.refinement_manifest_json, pretty_json_bytes(manifest), private=True
    )
    return RefinementResult(
        refinement_manifest=manifest,
        segments=segments,
        edits=tuple(ledger.records),
        ambiguities=tuple(ambiguities.records),
        reused=False,
    )


def _read_refined_segments(
    workspace: JobWorkspace,
    artifact: ArtifactRef,
    raw_segments: tuple[TranscriptSegment, ...],
    ledger: RefinementEditLedger,
) -> tuple[TranscriptSegment, ...]:
    path = workspace.safe_path(artifact.relative_storage_path)
    _verify_artifact(path, artifact)
    payload = path.read_bytes()
    if not payload.endswith(b"\n"):
        raise integrity_error("REFINEMENT_BUNDLE_INVALID", "refined transcript JSONL is incomplete")
    try:
        segments = tuple(
            TranscriptSegment.model_validate_json(line, strict=True)
            for line in payload.splitlines()
        )
    except ValidationError:
        raise integrity_error(
            "REFINEMENT_BUNDLE_INVALID", "refined transcript JSONL is malformed"
        ) from None
    raw_by_id = {item.segment_id: item for item in raw_segments}
    edits = {item.segment_id: item for item in ledger.records}
    if len(segments) != len(raw_segments) or [item.segment_id for item in segments] != [
        f"T{index:06d}" for index in range(1, len(segments) + 1)
    ]:
        raise integrity_error(
            "REFINEMENT_BUNDLE_INVALID", "refined transcript does not match raw input"
        )
    for item in segments:
        raw = raw_by_id.get(item.segment_id)
        if raw is None or item.model_copy(update={"normalized_text": raw.normalized_text}) != raw:
            raise integrity_error(
                "REFINEMENT_BUNDLE_INVALID", "refined transcript changed immutable raw fields"
            )
        expected = (
            edits[item.segment_id].refined_text if item.segment_id in edits else raw.normalized_text
        )
        if item.normalized_text != expected:
            raise integrity_error(
                "REFINEMENT_BUNDLE_INVALID", "refined transcript does not match its edit ledger"
            )
    return segments


def _load_ledgers(
    workspace: JobWorkspace, manifest: RefinementManifest
) -> tuple[RefinementEditLedger, RefinementAmbiguityLedger, RefinementResponseManifest]:
    edit_path = workspace.safe_path(manifest.edit_ledger_artifact.relative_storage_path)
    ambiguity_path = workspace.safe_path(manifest.ambiguity_artifact.relative_storage_path)
    response_path = workspace.safe_path(manifest.response_artifact.relative_storage_path)
    _verify_artifact(edit_path, manifest.edit_ledger_artifact)
    _verify_artifact(ambiguity_path, manifest.ambiguity_artifact)
    _verify_artifact(response_path, manifest.response_artifact)
    return (
        _read_model(edit_path, RefinementEditLedger, code="REFINEMENT_BUNDLE_INVALID"),
        _read_model(ambiguity_path, RefinementAmbiguityLedger, code="REFINEMENT_BUNDLE_INVALID"),
        _read_model(response_path, RefinementResponseManifest, code="REFINEMENT_BUNDLE_INVALID"),
    )


def load_refinement_result(
    workspace: JobWorkspace,
    job: JobSpec,
    media_manifest: MediaManifest | None,
    raw_manifest: TranscriptManifest,
    raw_segments: tuple[TranscriptSegment, ...],
    *,
    request: RefinementRequestManifest | None = None,
) -> RefinementResult:
    if request is None:
        if media_manifest is None:
            raise integrity_error("REFINEMENT_CONTEXT_MISSING", "media manifest is required")
        request = load_refinement_request(
            workspace, job, media_manifest, raw_manifest, raw_segments
        )
    manifest = _read_model(
        workspace.refinement_manifest_json,
        RefinementManifest,
        code="REFINEMENT_MANIFEST_INVALID",
    )
    ledger, ambiguities, response = _load_ledgers(workspace, manifest)
    segments = _read_refined_segments(
        workspace, manifest.refined_segments_artifact, raw_segments, ledger
    )
    if (
        manifest.job_id != job.job_id
        or manifest.raw_transcript_fingerprint != raw_manifest.transcription_fingerprint
        or manifest.raw_segments_artifact != raw_manifest.segments_artifact
        or manifest.request_fingerprint != request.request_fingerprint
        or manifest.response_fingerprint != fingerprint(response)
        or manifest.implementation_version != REFINEMENT_IMPLEMENTATION_VERSION
        or manifest.refined_transcript_fingerprint
        != refined_transcript_fingerprint(
            raw_manifest,
            manifest.refined_segments_artifact,
            manifest.edit_ledger_artifact,
            manifest.ambiguity_artifact,
            request,
            response,
        )
        or manifest.edit_count != len(ledger.records)
        or manifest.ambiguity_count != len(ambiguities.records)
    ):
        raise integrity_error("REFINEMENT_MANIFEST_MISMATCH", "refinement manifest is inconsistent")
    _validate_response(request, response, allow_fixture=True)
    return RefinementResult(
        refinement_manifest=manifest,
        segments=segments,
        edits=tuple(ledger.records),
        ambiguities=tuple(ambiguities.records),
        reused=True,
    )


def evidence_transcript_projection(
    raw_manifest: TranscriptManifest, refinement: RefinementResult
) -> TranscriptManifest:
    """Project refined segments into the existing evidence/search input contract."""

    data = raw_manifest.model_dump(mode="python")
    data.update(
        {
            "transcription_fingerprint": refinement.refinement_manifest.refined_transcript_fingerprint,
            "implementation_version": REFINEMENT_IMPLEMENTATION_VERSION,
            "segments_artifact": refinement.refinement_manifest.refined_segments_artifact,
            "warnings": sorted(set([*raw_manifest.warnings, "TRANSCRIPT_REFINED"])),
        }
    )
    return TranscriptManifest.model_validate(data)
