"""Deterministic PDF-page, image and notes projections for lectures without A/V."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from video_evidence_pipeline.domain.evidence import SourceRef, SourceRefKind
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.lecture import (
    LectureAsset,
    LectureDocumentIndex,
    LectureSource,
    SourceLocator,
)
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import configuration_error, integrity_error, io_error
from video_evidence_pipeline.supporting import _is_image

_MAX_PAGES_PER_PDF = 100
_MAX_TOTAL_ASSET_BYTES = 160 * 1024 * 1024
_MAX_RASTER_BYTES = 12_000_000


def _run(executable: Path, options: list[str], cwd: Path) -> None:
    try:
        result = subprocess.run(
            [str(executable), *options],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise io_error("LECTURE_PDF_EXTRACTION_FAILED", "PDF extraction was interrupted") from exc
    if result.returncode:
        raise io_error("LECTURE_PDF_EXTRACTION_FAILED", "PDF extraction failed")


def _text(path: Path) -> str:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise integrity_error("LECTURE_SIZE_LIMIT", "extracted lecture text exceeds its byte limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise integrity_error("LECTURE_DOCUMENT_INVALID", "document text is not UTF-8") from exc


def _pdf_pages(
    payload: bytes, temporary: Path, stem: str, tool_digests: dict[str, str]
) -> list[tuple[str, bytes]]:
    tools = {}
    for name in ("pdftotext", "pdftoppm"):
        executable = shutil.which(name)
        if not executable:
            raise configuration_error(
                "LECTURE_POPPLER_MISSING", "install Poppler pdftotext and pdftoppm for PDF input"
            )
        tools[name] = Path(executable).resolve(strict=True)
        digest = sha256_file(tools[name])
        if tool_digests.setdefault(name, digest) != digest:
            raise integrity_error("LECTURE_EXTRACTOR_CHANGED", "the PDF extractor changed")
    source = temporary / f"{stem}.pdf"
    text = temporary / f"{stem}.txt"
    source.write_bytes(payload)
    _run(
        tools["pdftotext"],
        [
            "-f",
            "1",
            "-l",
            str(_MAX_PAGES_PER_PDF + 1),
            "-layout",
            "-enc",
            "UTF-8",
            str(source),
            str(text),
        ],
        temporary,
    )
    pages = _text(text).split("\f")
    if pages[-1] == "":
        pages.pop()
    if not 0 < len(pages) <= _MAX_PAGES_PER_PDF:
        raise integrity_error(
            "LECTURE_PDF_PAGE_LIMIT", "PDF has no pages or exceeds the page limit"
        )
    _run(
        tools["pdftoppm"],
        [
            "-png",
            "-scale-to",
            "1600",
            "-f",
            "1",
            "-l",
            str(len(pages)),
            str(source),
            str(temporary / stem),
        ],
        temporary,
    )
    images = sorted(
        temporary.glob(f"{stem}-*.png"), key=lambda path: int(path.stem.rsplit("-", 1)[1])
    )
    if len(images) != len(pages) or any(
        not 0 < path.stat().st_size <= _MAX_RASTER_BYTES for path in images
    ):
        raise integrity_error("LECTURE_PDF_IMAGE_INVALID", "PDF page rasterization is incomplete")
    if sum(path.stat().st_size for path in images) > _MAX_TOTAL_ASSET_BYTES:
        raise integrity_error("LECTURE_SIZE_LIMIT", "PDF page images exceed their byte limit")
    if any(sha256_file(path) != tool_digests[name] for name, path in tools.items()):
        raise integrity_error("LECTURE_EXTRACTOR_CHANGED", "the PDF extractor changed")
    return [(text.strip(), image.read_bytes()) for text, image in zip(pages, images, strict=True)]


def build_document_evidence(
    index: LectureDocumentIndex,
    index_root: Path,
    temporary: Path,
    read_file: Callable[[Path, int], bytes],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    sources: list[LectureSource] = []
    payloads: dict[str, bytes] = {}
    assets: list[LectureAsset] = []
    originals: dict[str, str] = {}
    producers: dict[str, str] = {}
    total_bytes = 0
    for number, document in enumerate(index.sources, start=1):
        path = index_root / document.path
        payload = read_file(path, 64 * 1024 * 1024)
        digest = sha256_bytes(payload)
        if digest != document.sha256:
            raise integrity_error("LECTURE_DOCUMENT_CHANGED", "registered document SHA-256 changed")
        originals[document.id] = digest
        stem = f"document-{number:03d}"
        suffix = path.suffix.lower()
        if suffix == ".pdf" and payload.startswith(b"%PDF-"):
            records = [
                (f"{document.id}:P{page:04d}", page, text, f"{stem}-p{page:04d}.png", raster)
                for page, (text, raster) in enumerate(
                    _pdf_pages(payload, temporary, stem, producers), start=1
                )
            ]
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"} and _is_image(payload):
            records = [(document.id, None, "", f"{stem}{suffix}", payload)]
        elif suffix in {".md", ".txt"}:
            try:
                content = payload.decode("utf-8").strip()
            except UnicodeError as exc:
                raise integrity_error(
                    "LECTURE_DOCUMENT_INVALID", "lecture notes must be UTF-8"
                ) from exc
            if not content:
                raise integrity_error("LECTURE_DOCUMENT_INVALID", "lecture notes must have content")
            records = [(document.id, None, content, None, None)]
        else:
            raise configuration_error(
                "LECTURE_DOCUMENT_UNSUPPORTED", "document type is not supported"
            )

        for id_, page, excerpt, asset_path, raster in records:
            if len(excerpt) > 65536:
                raise integrity_error(
                    "LECTURE_SIZE_LIMIT", "document excerpt exceeds its character limit"
                )
            note = (
                None
                if excerpt
                else "Inspect the bound page image; text extraction returned no text."
            )
            title = document.title or path.name
            sources.append(
                LectureSource(
                    id=id_,
                    kind="current_lecture",
                    title=f"{title} p.{page}" if page is not None else title,
                    locator=SourceLocator(ref_id=id_, page=page, sha256=digest, note=note),
                    excerpt=excerpt or note,
                    source_refs=[
                        SourceRef(
                            kind=SourceRefKind.APPROVED_ARTIFACT,
                            ref_id=id_,
                            content_sha256=digest,
                        )
                    ],
                )
            )
            if asset_path and raster:
                if len(raster) > _MAX_RASTER_BYTES:
                    raise integrity_error(
                        "LECTURE_SIZE_LIMIT", "source image exceeds its byte limit"
                    )
                total_bytes += len(raster)
                if total_bytes > _MAX_TOTAL_ASSET_BYTES:
                    raise integrity_error(
                        "LECTURE_SIZE_LIMIT", "document images exceed their byte limit"
                    )
                payloads[asset_path] = raster
                assets.append(
                    LectureAsset(
                        path=asset_path,
                        sha256=sha256_bytes(raster),
                        size_bytes=len(raster),
                        source_refs=[id_],
                    )
                )
    return (
        {
            "job_id": index.job_id,
            "source_state": "DOCUMENTS_VERIFIED",
            "evidence_bundle_fingerprint": fingerprint(
                {
                    "sources": [item.model_dump(mode="json") for item in sources],
                    "assets": [item.model_dump(mode="json") for item in assets],
                    "documents": originals,
                    "extractors": producers,
                }
            ),
            "sources": sources,
            "crop_assets": assets,
            "external_api_allowed": False,
        },
        payloads,
    )
