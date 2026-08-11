"""Bounded local supporting-input publication for refinement and evidence."""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import JobSpec, SupportingInputKind
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


def _verify_artifact(path: Path, artifact: ArtifactRef) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != artifact.size_bytes
            or sha256_file(path) != artifact.content_sha256
        ):
            raise integrity_error("SUPPORTING_INPUT_TAMPERED", "supporting artifact is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "SUPPORTING_INPUT_TAMPERED", "supporting artifact is unavailable"
        ) from None


def _store(
    workspace: JobWorkspace,
    payload: bytes,
    *,
    input_id: str,
    artifact_type: str,
    directory: str,
    filename: str,
) -> ArtifactRef:
    content_sha256 = sha256_bytes(payload)
    artifact = ArtifactRef(
        artifact_id=f"{input_id.lower()}-{artifact_type}-{content_sha256[:16]}",
        artifact_type=artifact_type,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        relative_storage_path=f"supporting/{directory}/{content_sha256}/{filename}",
    )
    target = workspace.safe_path(artifact.relative_storage_path)
    for directory_path in (
        workspace.safe_path("supporting"),
        workspace.safe_path(f"supporting/{directory}"),
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


def _cheap_pdf_text(payload: bytes) -> bytes | None:
    """Use only literal PDF text operators; compressed/OCR content remains unavailable."""

    literals = re.findall(r"\(((?:\\.|[^\\)]){1,4096})\)\s*(?:Tj|'|\")", payload.decode("latin-1"))
    text = " ".join(
        value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\") for value in literals
    )
    return (text + "\n").encode("utf-8") if text else None


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
            text = _cheap_pdf_text(payload)
            if text is None:
                warnings.append("PDF_EMBEDDED_TEXT_UNAVAILABLE")
            else:
                text_artifact = _store(
                    workspace,
                    text,
                    input_id=input_id,
                    artifact_type="supporting_pdf_text",
                    directory="text",
                    filename="embedded-text.txt",
                )
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


def load_supporting_inputs(workspace: JobWorkspace, job: JobSpec) -> SupportingInputManifest:
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
        or manifest.supporting_fingerprint != supporting_fingerprint(job, manifest.inputs)
        or len(manifest.inputs) != len(job.supporting_inputs)
        or [item.kind for item in manifest.inputs] != [item.kind for item in job.supporting_inputs]
    ):
        raise state_error("SUPPORTING_INPUT_STALE", "supporting input manifest does not match job")
    for record in manifest.inputs:
        for artifact in (
            record.original_artifact,
            record.canonical_text_artifact,
            *(page.artifact for page in record.rendered_pages),
        ):
            if artifact is not None:
                _verify_artifact(workspace.safe_path(artifact.relative_storage_path), artifact)
    return manifest
