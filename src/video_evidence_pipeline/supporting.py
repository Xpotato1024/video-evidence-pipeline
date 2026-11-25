"""Bounded local supporting-input publication for refinement and evidence."""

from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from video_evidence_pipeline.domain.artifacts import ArtifactRef, artifact_file_matches
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import (
    RECOVERY_CLARIFICATION_PREFIX,
    JobSpec,
    SupportingInputKind,
)
from video_evidence_pipeline.domain.recovery import RecoveryClarificationPacket
from video_evidence_pipeline.domain.serialization import fingerprint, pretty_json_bytes
from video_evidence_pipeline.domain.supporting import (
    RenderedPdfPage,
    SupportingInputManifest,
    SupportingInputRecord,
)
from video_evidence_pipeline.errors import VepError, integrity_error, state_error
from video_evidence_pipeline.storage.atomic import atomic_write_new_bytes, read_json_model
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    enforce_private_file,
    ensure_private_directory,
)

SUPPORTING_INPUT_IMPLEMENTATION_VERSION = "supporting-inputs-v1"
CLARIFICATION_ORIGINAL = "supporting_clarification_original"
CLARIFICATION_HISTORY = "supporting_clarification_history"
CLARIFICATION_CONFIRMATIONS = "supporting_clarification_confirmations"
_MAXIMUM_BINARY_BYTES = 512 * 1024 * 1024
_MAXIMUM_TEXT_BYTES = 64 * 1024 * 1024
_MAXIMUM_RENDERED_PAGE_BYTES = 32 * 1024 * 1024


def _is_pdf(payload: bytes) -> bool:
    return payload.startswith(b"%PDF-")


def _is_image(payload: bytes) -> bool:
    return (
        payload.startswith(b"\x89PNG\r\n\x1a\n")
        or payload.startswith(b"\xff\xd8\xff")
        or payload.startswith((b"GIF87a", b"GIF89a"))
        or (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")
    )


def _verify_artifact(path: Path, artifact: ArtifactRef, *, deep: bool = False) -> None:
    try:
        if not artifact_file_matches(path, artifact, deep=deep, hasher=sha256_file):
            raise integrity_error("SUPPORTING_INPUT_TAMPERED", "supporting artifact is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "SUPPORTING_INPUT_TAMPERED", "supporting artifact is unavailable"
        ) from None


def _supporting_artifact(
    payload: bytes,
    *,
    input_id: str,
    artifact_type: str,
    directory: str,
    filename: str,
) -> ArtifactRef:
    content_sha256 = sha256_bytes(payload)
    return ArtifactRef(
        artifact_id=f"{input_id.lower()}-{artifact_type}-{content_sha256[:16]}",
        artifact_type=artifact_type,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        relative_storage_path=f"supporting/{directory}/{content_sha256}/{filename}",
    )


def _publish_artifact(workspace: JobWorkspace, artifact: ArtifactRef, payload: bytes) -> None:
    target = workspace.safe_path(artifact.relative_storage_path)
    directory = workspace.root
    for component in target.parent.relative_to(workspace.root).parts:
        directory /= component
        ensure_private_directory(directory)
    try:
        atomic_write_new_bytes(target, payload, private=True)
    except VepError as exc:
        if exc.code != "FILE_EXISTS":
            raise
    _verify_artifact(target, artifact)


def _store(
    workspace: JobWorkspace,
    payload: bytes,
    *,
    input_id: str,
    artifact_type: str,
    directory: str,
    filename: str,
) -> ArtifactRef:
    artifact = _supporting_artifact(
        payload,
        input_id=input_id,
        artifact_type=artifact_type,
        directory=directory,
        filename=filename,
    )
    _publish_artifact(workspace, artifact, payload)
    return artifact


def recovery_clarification_note(payload: bytes, *, input_index: int) -> SupportingInputRecord:
    """Describe exact original and text bytes for attributed clarification JSON."""
    if not 0 < len(payload) <= _MAXIMUM_TEXT_BYTES:
        raise state_error("RECOVERY_CLARIFICATIONS_TOO_LARGE", "clarification packet is too large")
    input_id = f"S{input_index:06d}"
    effective = _clarification_confirmations(payload)
    return SupportingInputRecord(
        input_id=input_id,
        kind=SupportingInputKind.NOTES,
        original_artifact=_supporting_artifact(
            payload,
            input_id=input_id,
            artifact_type=CLARIFICATION_ORIGINAL,
            directory="original",
            filename="source.bin",
        ),
        canonical_text_artifact=_supporting_artifact(
            effective if effective is not None else payload,
            input_id=input_id,
            artifact_type=CLARIFICATION_CONFIRMATIONS
            if effective is not None
            else CLARIFICATION_HISTORY,
            directory="text",
            filename="clarifications.json",
        ),
    )


def _clarification_confirmations(payload: bytes) -> bytes | None:
    packet = RecoveryClarificationPacket.model_validate_json(payload)
    active = set(packet.active_resolutions.values())
    if not active:
        return None
    return pretty_json_bytes(
        {
            "role": "effective_clarification_confirmations",
            "parent_job_id": packet.parent_job_id,
            "confirmations": [
                item.model_dump(mode="json")
                for item in packet.clarifications
                if item.fingerprint in active
            ],
        }
    )


def publish_recovery_clarification_note(
    workspace: JobWorkspace, record: SupportingInputRecord, payload: bytes
) -> None:
    _publish_artifact(workspace, record.original_artifact, payload)
    text = record.canonical_text_artifact
    assert text is not None
    effective = _clarification_confirmations(payload)
    _publish_artifact(workspace, text, effective if effective is not None else payload)


def verify_recovery_clarification_note(
    workspace: JobWorkspace, record: SupportingInputRecord
) -> None:
    for artifact in (record.original_artifact, record.canonical_text_artifact):
        if artifact is not None:
            _verify_artifact(
                workspace.safe_path(artifact.relative_storage_path), artifact, deep=True
            )


def _read_local(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise integrity_error("SUPPORTING_INPUT_INVALID", "supporting input is invalid")
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError:
        raise integrity_error(
            "SUPPORTING_INPUT_INVALID", "supporting input is unavailable"
        ) from None
    if len(payload) != before.st_size or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise integrity_error("SUPPORTING_INPUT_INVALID", "supporting input changed while reading")
    return payload


def _canonical_notes(payload: bytes) -> bytes:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise integrity_error("SUPPORTING_NOTES_INVALID", "notes must be UTF-8 text") from None
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    if not text:
        raise integrity_error("SUPPORTING_NOTES_INVALID", "notes must not be blank")
    return (text + "\n").encode("utf-8")


def _render_first_pdf_page(workspace: JobWorkspace, payload: bytes) -> bytes | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    root = workspace.safe_path("supporting/staging")
    ensure_private_directory(root)
    try:
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            source = Path(temporary) / "source.pdf"
            source.write_bytes(payload)
            base = Path(temporary) / "page"
            completed = subprocess.run(
                [
                    executable,
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-singlefile",
                    "-png",
                    str(source),
                    str(base),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            page = base.with_suffix(".png")
            if (
                completed.returncode != 0
                or not page.is_file()
                or page.stat(follow_symlinks=False).st_size > _MAXIMUM_RENDERED_PAGE_BYTES
            ):
                return None
            return page.read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        return None


def supporting_fingerprint(job: JobSpec, records: list[SupportingInputRecord]) -> str:
    return fingerprint(
        {
            "job_id": job.job_id,
            "inputs": [item.model_dump(mode="json") for item in records],
            "implementation_version": SUPPORTING_INPUT_IMPLEMENTATION_VERSION,
        }
    )


def _records(workspace: JobWorkspace, job: JobSpec) -> list[SupportingInputRecord]:
    records: list[SupportingInputRecord] = []
    for index, specification in enumerate(job.supporting_inputs, start=1):
        if specification.locator.startswith(RECOVERY_CLARIFICATION_PREFIX):
            raise integrity_error(
                "RECOVERY_CLARIFICATION_SOURCE_INVALID",
                "a generated clarification note requires its recovery manifest",
            )
        input_id = f"S{index:06d}"
        source = Path(specification.locator)
        payload = _read_local(
            source,
            maximum_bytes=(
                _MAXIMUM_TEXT_BYTES
                if specification.kind is SupportingInputKind.NOTES
                else _MAXIMUM_BINARY_BYTES
            ),
        )
        if specification.kind is SupportingInputKind.PDF and not _is_pdf(payload):
            raise integrity_error("SUPPORTING_PDF_INVALID", "PDF input has an invalid signature")
        if specification.kind is SupportingInputKind.IMAGE and not _is_image(payload):
            raise integrity_error(
                "SUPPORTING_IMAGE_INVALID", "image input has an invalid signature"
            )
        original = _store(
            workspace,
            payload,
            input_id=input_id,
            artifact_type=f"supporting_{specification.kind.value}_original",
            directory="original",
            filename="source.bin",
        )
        text_artifact = None
        pages: list[RenderedPdfPage] = []
        warnings: list[str] = []
        if specification.kind is SupportingInputKind.NOTES:
            text_artifact = _store(
                workspace,
                _canonical_notes(payload),
                input_id=input_id,
                artifact_type="supporting_notes_text",
                directory="text",
                filename="notes.txt",
            )
        elif specification.kind is SupportingInputKind.PDF:
            warnings.append("PDF_EMBEDDED_TEXT_UNAVAILABLE")
            page = _render_first_pdf_page(workspace, payload)
            if page is None:
                warnings.append("PDF_PAGE_RENDER_UNAVAILABLE")
            else:
                pages.append(
                    RenderedPdfPage(
                        page_number=1,
                        artifact=_store(
                            workspace,
                            page,
                            input_id=input_id,
                            artifact_type="supporting_pdf_page",
                            directory="pages",
                            filename="page-0001.png",
                        ),
                    )
                )
        records.append(
            SupportingInputRecord(
                input_id=input_id,
                kind=specification.kind,
                original_artifact=original,
                canonical_text_artifact=text_artifact,
                rendered_pages=pages,
                warnings=warnings,
            )
        )
    return records


def prepare_supporting_inputs(workspace: JobWorkspace, job: JobSpec) -> SupportingInputManifest:
    """Publish only the three declared local supporting kinds."""

    if workspace.supporting_manifest_json.exists():
        return load_supporting_inputs(workspace, job)
    records = _records(workspace, job)
    manifest = SupportingInputManifest(
        job_id=job.job_id,
        supporting_fingerprint=supporting_fingerprint(job, records),
        implementation_version=SUPPORTING_INPUT_IMPLEMENTATION_VERSION,
        inputs=records,
        created_at=datetime.now(UTC),
    )
    try:
        atomic_write_new_bytes(
            workspace.supporting_manifest_json, pretty_json_bytes(manifest), private=True
        )
    except VepError as exc:
        if exc.code != "FILE_EXISTS":
            raise
    return load_supporting_inputs(workspace, job)


def load_supporting_inputs(
    workspace: JobWorkspace, job: JobSpec, *, deep: bool = False
) -> SupportingInputManifest:
    try:
        manifest = read_json_model(
            workspace.supporting_manifest_json,
            SupportingInputManifest,
            code="SUPPORTING_INPUT_MANIFEST_INVALID",
        )
    except ValidationError:
        raise integrity_error(
            "SUPPORTING_INPUT_MANIFEST_INVALID", "supporting input manifest is invalid"
        ) from None
    if (
        manifest.job_id != job.job_id
        or manifest.implementation_version != SUPPORTING_INPUT_IMPLEMENTATION_VERSION
        or (
            deep and manifest.supporting_fingerprint != supporting_fingerprint(job, manifest.inputs)
        )
        or len(manifest.inputs) != len(job.supporting_inputs)
        or [item.kind for item in manifest.inputs] != [item.kind for item in job.supporting_inputs]
    ):
        raise state_error("SUPPORTING_INPUT_STALE", "supporting input manifest does not match job")
    for spec, record in zip(job.supporting_inputs, manifest.inputs, strict=True):
        text = record.canonical_text_artifact
        clarification = record.original_artifact.artifact_type == CLARIFICATION_ORIGINAL
        if (
            clarification != spec.locator.startswith(RECOVERY_CLARIFICATION_PREFIX)
            or (
                clarification
                and spec.locator
                != (
                    f"{RECOVERY_CLARIFICATION_PREFIX}{record.input_id[1:]}/"
                    f"{record.original_artifact.content_sha256}"
                )
            )
            or (
                text is not None
                and clarification
                != (text.artifact_type in {CLARIFICATION_CONFIRMATIONS, CLARIFICATION_HISTORY})
            )
        ):
            raise integrity_error(
                "RECOVERY_CLARIFICATION_SOURCE_INVALID", "clarification input binding is invalid"
            )
        for artifact in (
            record.original_artifact,
            record.canonical_text_artifact,
            *(page.artifact for page in record.rendered_pages),
        ):
            if artifact is not None:
                _verify_artifact(
                    workspace.safe_path(artifact.relative_storage_path), artifact, deep=deep
                )
    return manifest
