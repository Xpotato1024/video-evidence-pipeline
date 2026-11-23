"""Freeze evidence, render a lecture candidate and publish a reviewed immutable package."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline import __version__
from video_evidence_pipeline.acquisition.process_tree import ManagedProcessTree
from video_evidence_pipeline.application import (
    _authorization,
    _load_context,
    _load_fixed_evidence,
    _require_current_authorization,
    _require_local_processing,
)
from video_evidence_pipeline.delivery.lecture_documents import build_document_evidence
from video_evidence_pipeline.delivery.lecture_source import apply_mdx_edits
from video_evidence_pipeline.delivery.study_bundle import validate_study_bundle
from video_evidence_pipeline.domain.authoring import AgentIdentity, AgentRole, AuditSeverity
from video_evidence_pipeline.domain.delivery import ReviewerIdentity
from video_evidence_pipeline.domain.evidence import (
    AmbiguityLedger,
    AssignmentRequirement,
    EvidenceRecord,
    SourceRef,
    SourceRefKind,
)
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.job import AssignmentSpec
from video_evidence_pipeline.domain.lecture import (
    ENTRYPOINTS,
    AuditKind,
    FigureBlock,
    InventoryFile,
    LectureApproval,
    LectureAsset,
    LectureAuditResponse,
    LectureDocumentIndex,
    LectureInput,
    LectureRequest,
    LectureSource,
    RichContent,
    RichDeliverableManifest,
    SourceLocator,
    StepsBlock,
    payload_fingerprint,
)
from video_evidence_pipeline.domain.serialization import fingerprint, pretty_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.study_bundle import StudySourceIndex, portable_path
from video_evidence_pipeline.errors import (
    configuration_error,
    integrity_error,
    io_error,
    state_error,
)
from video_evidence_pipeline.runtime_context import resource_root
from video_evidence_pipeline.storage.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_new_bytes,
)
from video_evidence_pipeline.storage.locking import exclusive_file_lock

_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_PACKAGE_BYTES = 256 * 1024 * 1024
_ZERO_SHA = "0" * 64
_TOPICS = {
    "concepts",
    "definitions",
    "derivations",
    "conditions",
    "teacher_emphasis",
    "assignments",
    "next_lesson",
    "worked_examples",
    "visuals",
    "prior_lessons",
}
_RENDER_FILES = {
    "lesson.mdx",
    "lesson.md",
    "review.html",
    "review.pdf",
    "render-receipt.json",
    "sites/index.html",
}
_EVIDENCE_STATES = {
    JobState.EVIDENCE_READY,
    JobState.DRAFTED,
    JobState.AUDITED,
    JobState.REVISION_REQUIRED,
    JobState.HUMAN_REVIEW_READY,
    JobState.HUMAN_APPROVED,
    JobState.EXPORTED,
}
_RASTER_SUFFIX = {".png", ".jpg", ".jpeg", ".webp"}


def _safe_absolute(path: Path) -> Path:
    full = path.absolute()
    if ".." in full.parts or any(
        part.is_symlink() or getattr(part, "is_junction", lambda: False)()
        for part in (full, *full.parents)
    ):
        raise integrity_error("LECTURE_PATH_UNSAFE", "lecture paths must be local and link-free")
    return full


def _read(path: Path, limit: int = _MAX_JSON_BYTES) -> bytes:
    path = _safe_absolute(path)
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise OSError
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise io_error("LECTURE_FILE_UNREADABLE", "lecture input file could not be read") from exc
    if len(data) > limit:
        raise integrity_error("LECTURE_SIZE_LIMIT", "lecture input exceeds its byte limit")
    return data


def _read_model[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        return model.model_validate_json(_read(path))
    except ValidationError as exc:
        raise integrity_error(
            "LECTURE_SCHEMA_INVALID", f"{model.__name__} input violates its contract"
        ) from exc


def _ledger[M: BaseModel](path: Path, model: type[M]) -> list[M]:
    raw = _read(path)
    if raw and not raw.endswith(b"\n"):
        raise integrity_error("LECTURE_LEDGER_INVALID", "legacy evidence ledger is incomplete")
    try:
        return [model.model_validate_json(line) for line in raw.splitlines()]
    except ValidationError as exc:
        raise integrity_error(
            "LECTURE_LEDGER_INVALID", "legacy evidence ledger is malformed"
        ) from exc


@contextmanager
def _stage(destination: Path) -> Iterator[Path]:
    destination = _safe_absolute(destination)
    if not destination.parent.is_dir():
        raise configuration_error("LECTURE_PARENT_MISSING", "create the destination parent first")
    with exclusive_file_lock(
        destination.with_name(f".{destination.name}.vep-lecture.lock"),
        busy_code="LECTURE_DESTINATION_BUSY",
        busy_message="lecture destination is in use",
        failure_code="LECTURE_LOCK_FAILED",
        failure_message="lecture destination lock failed",
    ):
        if destination.exists():
            raise configuration_error(
                "LECTURE_DESTINATION_EXISTS", "lecture destinations are immutable; use a new path"
            )
        try:
            with tempfile.TemporaryDirectory(
                prefix=".vep-lecture-", dir=destination.parent
            ) as temp:
                stage = Path(temp) / "payload"
                stage.mkdir(mode=0o700)
                yield stage
                if destination.exists():
                    raise configuration_error(
                        "LECTURE_DESTINATION_EXISTS", "lecture destination already exists"
                    )
                stage.rename(destination)
        except OSError as exc:
            raise io_error(
                "LECTURE_PUBLICATION_FAILED", "lecture stage could not be published"
            ) from exc


def _inventory(root: Path, *, exclude: set[str] | None = None) -> list[InventoryFile]:
    root = _safe_absolute(root)
    if not root.is_dir():
        raise integrity_error("LECTURE_INVENTORY_INVALID", "lecture bundle directory is missing")
    files = []
    paths = set()
    pending = [root]
    entry_count = total_bytes = 0
    while pending:
        for child in pending.pop().iterdir():
            entry_count += 1
            if entry_count > 8192:
                raise integrity_error("LECTURE_SIZE_LIMIT", "lecture package has too many entries")
            mode = child.lstat().st_mode
            relative = child.relative_to(root).as_posix()
            portable_path(relative)
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                raise integrity_error("LECTURE_PATH_UNSAFE", "lecture bundles cannot contain links")
            folded = relative.casefold()
            if folded in paths:
                raise integrity_error("LECTURE_PATH_UNSAFE", "lecture paths must not collide")
            paths.add(folded)
            if stat.S_ISDIR(mode):
                pending.append(child)
                continue
            if not stat.S_ISREG(mode):
                raise integrity_error(
                    "LECTURE_PATH_UNSAFE", "lecture outputs must be regular files"
                )
            if relative in (exclude or set()):
                continue
            with child.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
            total_bytes += size
            if not 0 < size <= 128 * 1024 * 1024 or total_bytes > _MAX_PACKAGE_BYTES:
                raise integrity_error(
                    "LECTURE_SIZE_LIMIT", "lecture package exceeds its byte limit"
                )
            files.append(InventoryFile(path=relative, size_bytes=size, sha256=sha256_file(child)))
    if len(files) > 4096:
        raise integrity_error("LECTURE_SIZE_LIMIT", "lecture package has too many files")
    return sorted(files, key=lambda item: item.path)


def _current_sources(records: list[EvidenceRecord]) -> list[LectureSource]:
    sources = []
    for record in records:
        first = record.source_refs[0]
        _, separator, page = first.ref_id.partition(":P")
        sources.append(
            LectureSource(
                id=record.evidence_id,
                kind="current_lecture",
                title=f"{first.kind.value} {first.ref_id}",
                locator=SourceLocator(
                    ref_id=first.ref_id,
                    page=int(page) if separator and page.isdigit() else None,
                    start_ms=first.start_ms,
                    end_ms=first.end_ms,
                    sha256=first.content_sha256,
                ),
                excerpt=record.observation,
                source_refs=record.source_refs,
                evidence_roles=[flag for flag in record.flags if flag.startswith("role:")],
            )
        )
    if not sources:
        raise state_error(
            "LECTURE_EVIDENCE_MISSING", "lecture authoring requires supported evidence"
        )
    return sources


def _crop_assets(
    records: list[EvidenceRecord],
    references: list[tuple[SourceRef, str, int, str]],
) -> list[LectureAsset]:
    by_ref: dict[str, list[str]] = {}
    for record in records:
        for source in record.source_refs:
            by_ref.setdefault(fingerprint(source), []).append(record.evidence_id)
    return sorted(
        (
            LectureAsset(path=path, size_bytes=size, sha256=digest, source_refs=ids)
            for source, path, size, digest in references
            if source.kind in {SourceRefKind.FRAME, SourceRefKind.PDF_PAGE, SourceRefKind.IMAGE}
            and (
                Path(path).suffix.lower() in _RASTER_SUFFIX
                or (source.kind == SourceRefKind.IMAGE and Path(path).suffix.lower() == ".bin")
            )
            and (ids := by_ref.get(fingerprint(source)))
            and size <= 12_000_000
        ),
        key=lambda item: item.path,
    )


def _archive_evidence(path: Path) -> dict[str, Any]:
    archive = validate_study_bundle(path)
    records = _ledger(path / archive.entrypoints["evidence"], EvidenceRecord)
    try:
        provenance = json.loads(_read(path / "provenance.json"))
        assignment = AssignmentSpec.model_validate(provenance["assignment"])
    except (KeyError, TypeError, ValueError) as exc:
        raise integrity_error(
            "LECTURE_ARCHIVE_INVALID", "legacy assignment provenance is invalid"
        ) from exc
    index = _read_model(path / archive.entrypoints["source_index"], StudySourceIndex)
    inventory = {item.relative_path: item for item in archive.files}
    return {
        "job_id": archive.job_id,
        "source_state": "APPROVED_ARCHIVE",
        "evidence_bundle_fingerprint": provenance["fixed_evidence_fingerprint"],
        "requirements": [
            AssignmentRequirement(requirement_id=f"R{number:06d}", text=text)
            for number, text in enumerate(assignment.requirements, start=1)
        ],
        "sources": _current_sources(records),
        "ambiguities": _read_model(
            path / archive.entrypoints["ambiguities"], AmbiguityLedger
        ).records,
        "crop_assets": _crop_assets(
            records,
            [
                (
                    item.source_ref,
                    item.relative_path,
                    inventory[item.relative_path].size_bytes,
                    inventory[item.relative_path].sha256,
                )
                for item in index.bindings
                if item.relative_path in inventory
            ],
        ),
        "legacy_draft_sha256": archive.approved_draft_sha256,
        "external_api_allowed": False,
    }


def _workspace_evidence(path: Path) -> dict[str, Any]:
    workspace, job, transitions, state = _load_context(path)
    if state not in _EVIDENCE_STATES:
        raise state_error("LECTURE_EVIDENCE_MISSING", "lecture jobs require verified VEP evidence")
    auth = _authorization(workspace)
    _require_current_authorization(job, transitions, auth)
    _require_local_processing(auth)
    fixed = _load_fixed_evidence(workspace, job, deep=True)
    evidence = fixed.evidence_request
    assets = [
        *zip(evidence.approved_artifact_refs, evidence.approved_artifacts, strict=True),
        *zip(
            evidence.frame_refs, (frame.artifact for frame in evidence.frame_records), strict=True
        ),
    ]
    return {
        "job_id": job.job_id,
        "source_state": state.value,
        "evidence_bundle_fingerprint": fixed.fixed_evidence_fingerprint,
        "sources": _current_sources(fixed.evidence_records),
        "ambiguities": fixed.ambiguity_records,
        "requirements": evidence.assignment.requirements,
        "crop_assets": _crop_assets(
            fixed.evidence_records,
            [
                (source, item.relative_storage_path, item.size_bytes, item.content_sha256)
                for source, item in assets
            ],
        ),
        "external_api_allowed": auth.allow_external_api,
    }


def prepare_lecture(
    workspace: Path,
    input_path: Path,
    destination: Path,
    *,
    archive: bool = False,
    documents: bool = False,
    document_index: Path | None = None,
) -> tuple[LectureRequest, bool]:
    if documents and (archive or document_index is not None):
        raise configuration_error(
            "LECTURE_SOURCE_MODE_INVALID", "select a single lecture source mode"
        )
    options = _read_model(input_path, LectureInput)
    source_images: dict[str, bytes] = {}
    if documents:
        document_index = workspace
        evidence: dict[str, Any] = {}
    else:
        evidence = _archive_evidence(workspace) if archive else _workspace_evidence(workspace)
    if document_index is not None:
        index = _read_model(document_index, LectureDocumentIndex)
        with tempfile.TemporaryDirectory(prefix="vep-documents-") as temporary:
            supporting, source_images = build_document_evidence(
                index, document_index.absolute().parent, Path(temporary), _read
            )
        if documents:
            evidence = supporting
        else:
            if evidence["job_id"] != supporting["job_id"]:
                raise configuration_error(
                    "LECTURE_DOCUMENT_JOB_MISMATCH", "document index belongs to a different job"
                )
            evidence["sources"] += supporting["sources"]
            evidence["crop_assets"] += supporting["crop_assets"]
            evidence["external_api_allowed"] = False
            evidence["evidence_bundle_fingerprint"] = fingerprint(
                {
                    "job_evidence": evidence["evidence_bundle_fingerprint"],
                    "documents": supporting["evidence_bundle_fingerprint"],
                }
            )
    evidence["sources"] += options.previous_context
    if len({item.id for item in evidence["sources"]}) != len(evidence["sources"]):
        raise configuration_error("LECTURE_SOURCE_COLLISION", "lecture source IDs collide")
    request = LectureRequest(
        **evidence,
        version=options.version,
        output_mode=options.output_mode,
        profile=options.profile,
        profile_snapshot_sha256=fingerprint(options.profile),
        request_fingerprint=_ZERO_SHA,
    )
    request = request.model_copy(
        update={"request_fingerprint": payload_fingerprint(request, "request_fingerprint")}
    )
    target = _safe_absolute(destination) / "request.json"
    if target.parent.exists():
        if _read_model(target, LectureRequest) == request:
            if source_images:
                found = _inventory(target.parent / "source-assets")
                if found != sorted(
                    (
                        InventoryFile(
                            path=item.path, sha256=item.sha256, size_bytes=item.size_bytes
                        )
                        for item in request.crop_assets
                        if item.path in source_images
                    ),
                    key=lambda item: item.path,
                ):
                    raise integrity_error("LECTURE_CROP_UNBOUND", "prepared source images changed")
            return request, True
        raise integrity_error(
            "LECTURE_REQUEST_STALE", "existing request binds different evidence or profile"
        )
    with _stage(destination) as stage:
        for relative, data in source_images.items():
            atomic_write_bytes(stage / "source-assets" / relative, data, private=True)
        request_bytes = pretty_json_bytes(request)
        if len(request_bytes) > _MAX_JSON_BYTES:
            raise integrity_error(
                "LECTURE_SIZE_LIMIT", "lecture evidence request exceeds its byte limit"
            )
        atomic_write_bytes(stage / "request.json", request_bytes, private=True)
    return request, False


def validate_lecture_request(path: Path) -> LectureRequest:
    request = _read_model(path, LectureRequest)
    _check_request(request)
    return request


def _check_request(request: LectureRequest) -> None:
    ids = {source.id for source in request.sources}
    asset_paths = {asset.path for asset in request.crop_assets}
    if (
        request.request_fingerprint != payload_fingerprint(request, "request_fingerprint")
        or request.profile_snapshot_sha256 != fingerprint(request.profile)
        or len(ids) != len(request.sources)
        or any(source.kind == "supplement" for source in request.sources)
        or not any(source.kind == "current_lecture" for source in request.sources)
        or len(asset_paths) != len(request.crop_assets)
        or any(not set(asset.source_refs) <= ids for asset in request.crop_assets)
    ):
        raise integrity_error(
            "LECTURE_REQUEST_STALE", "lecture request identity or evidence is inconsistent"
        )


def _check_content(content: RichContent, request: LectureRequest) -> None:
    _check_request(request)
    if (
        content.request_fingerprint != request.request_fingerprint
        or content.output_mode != request.output_mode
        or content.language != request.profile.language
    ):
        raise integrity_error("LECTURE_REQUEST_STALE", "lecture does not match its frozen request")
    if content.author.external_api_used and not request.external_api_allowed:
        raise state_error(
            "LECTURE_EXTERNAL_API_DENIED", "external lecture processing is unauthorized"
        )
    frozen = {source.id: source for source in request.sources}
    if any(
        frozen.get(source.id) != source
        for source in content.sources
        if source.kind != "supplement" or source.id in frozen
    ):
        raise integrity_error("LECTURE_SOURCE_MISMATCH", "sources differ from the frozen evidence")
    if {item.topic for item in content.coverage} != _TOPICS:
        raise integrity_error(
            "LECTURE_COVERAGE_INCOMPLETE", "lecture coverage accounts for every topic"
        )
    represented = {ref for block in content.blocks for ref in block.source_refs}
    emphases = {source.id for source in request.sources if "role:emphasis" in source.evidence_roles}
    if emphases - represented:
        raise integrity_error("LECTURE_EMPHASIS_MISSING", "explicit teaching emphasis is missing")
    by_topic = {item.topic: item for item in content.coverage}
    by_block = {block.id: block for block in content.blocks}
    for topic, block_type in (("worked_examples", StepsBlock), ("visuals", FigureBlock)):
        coverage = by_topic[topic]
        if coverage.status == "covered" and not any(
            isinstance(by_block[block_id], block_type) for block_id in coverage.block_ids
        ):
            raise integrity_error(
                "LECTURE_COVERAGE_INCOMPLETE", "coverage lacks the relevant artifact"
            )
    if request.output_mode == "worked_explanation" and not all(
        by_topic[topic].status == "covered" for topic in ("concepts", "definitions")
    ):
        raise integrity_error(
            "LECTURE_COVERAGE_INCOMPLETE", "lecture lacks key concepts or definitions"
        )


def _renderer() -> Path:
    repo = Path(__file__).resolve().parents[3]
    path = resource_root(repo) / "renderer" / "render.mjs"
    if not path.is_file():
        raise configuration_error("LECTURE_RENDERER_MISSING", "install the VEP lecture renderer")
    return path


def _render(content_path: Path, stage: Path, *, assets: Path | None) -> None:
    node = shutil.which("node")
    if not node:
        raise configuration_error(
            "LECTURE_NODE_MISSING", "Node.js is required for lecture rendering"
        )
    command = [node, str(_renderer()), "--input", str(content_path), "--output", str(stage)]
    if assets is not None:
        command.extend(["--assets", str(assets)])
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "PLAYWRIGHT_BROWSERS_PATH",
            "VEP_CHROMIUM_PATH",
            "VEP_CHROMIUM_SANDBOX",
            "LD_LIBRARY_PATH",
            "XDG_CACHE_HOME",
            "FONTCONFIG_PATH",
        }
    }
    child_env["TZ"] = "UTC"
    try:
        process = ManagedProcessTree.start(
            command,
            process_factory=subprocess.Popen,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=stage,
            env=child_env,
            shell=False,
        )
    except OSError as exc:
        raise io_error("LECTURE_RENDER_UNAVAILABLE", "lecture renderer could not start") from exc
    try:
        returncode = process.wait(timeout=180)
    except subprocess.TimeoutExpired as exc:
        process.stop()
        raise io_error("LECTURE_RENDER_TIMEOUT", "lecture renderer timed out") from exc
    finally:
        process.close()
    if returncode:
        raise io_error(
            "LECTURE_RENDER_FAILED",
            "lecture renderer returned an error; run the rendering command directly for input diagnostics",
        )


def _selected_assets(content: RichContent, request: LectureRequest) -> dict[str, LectureAsset]:
    catalog = {item.path: item for item in request.crop_assets}
    selected = {}
    for figure in content.figures:
        if figure.kind != "source_crop":
            continue
        path = figure.spec.get("path")
        if not isinstance(path, str) or path not in catalog:
            raise integrity_error(
                "LECTURE_CROP_UNBOUND", "crop does not reference verified evidence"
            )
        asset = catalog[path]
        if not set(figure.source_refs) & set(asset.source_refs):
            raise integrity_error("LECTURE_CROP_UNBOUND", "crop evidence binding does not match")
        selected[path] = asset
    return selected


def render_lecture(
    request_path: Path, content_path: Path, destination: Path, *, assets: Path | None = None
) -> tuple[str, bool]:
    request = _read_model(request_path, LectureRequest)
    content = _read_model(content_path, RichContent)
    _check_content(content, request)
    selected = _selected_assets(content, request)
    prepared_assets = request_path.parent / "source-assets"
    if selected and assets is None and not prepared_assets.is_dir():
        raise configuration_error(
            "LECTURE_ASSETS_REQUIRED", "provide the verified evidence asset root"
        )
    destination = _safe_absolute(destination)
    if destination.exists():
        existing_request, existing_content, inventory = _load_render(destination)
        if (existing_request, existing_content) == (request, content):
            return fingerprint(inventory), True
        raise integrity_error(
            "LECTURE_RENDER_STALE", "existing render binds different lecture inputs"
        )
    with _stage(destination) as stage:
        inputs = stage.parent / "content.json"
        atomic_write_json(inputs, content, private=True)
        isolated_assets = stage.parent / "assets"
        for path, item in selected.items():
            source = prepared_assets / path
            if not source.exists():
                if assets is None:
                    raise configuration_error(
                        "LECTURE_ASSETS_REQUIRED", "provide the verified evidence asset root"
                    )
                source = assets / path
            data = _read(source, item.size_bytes)
            target = isolated_assets / path
            atomic_write_bytes(target, data, private=True)
            if target.stat().st_size != item.size_bytes or sha256_file(target) != item.sha256:
                raise integrity_error("LECTURE_CROP_UNBOUND", "crop source identity changed")
        _render(inputs, stage, assets=isolated_assets if selected else None)
        atomic_write_bytes(stage / "content.json", inputs.read_bytes(), private=True)
        atomic_write_json(stage / "authoring-request.json", request, private=True)
        inventory = _render_inventory(stage)
        atomic_write_json(stage / "render-inventory.json", inventory, private=True)
    return fingerprint(inventory), False


def _render_inventory(root: Path) -> list[dict[str, Any]]:
    inventory = _inventory(root, exclude={"render-inventory.json"})
    paths = {item.path for item in inventory}
    if not paths >= _RENDER_FILES:
        raise integrity_error(
            "LECTURE_RENDER_INCOMPLETE", "required lecture render files are missing"
        )
    if any(
        path not in _RENDER_FILES | {"content.json", "authoring-request.json"}
        and not path.startswith(("figures/", "math/", "data/", "sites/", "inputs/"))
        for path in paths
    ):
        raise integrity_error("LECTURE_RENDER_INVALID", "unexpected file in lecture render output")
    by_path = {item.path: item for item in inventory}
    try:
        receipt = json.loads(_read(root / "render-receipt.json"))
        described = {
            item.path: {"sha256": item.sha256, "size_bytes": item.size_bytes}
            for item in inventory
            if item.path not in {"content.json", "authoring-request.json", "render-receipt.json"}
        }
        if (
            receipt["schema_version"] != "2.0"
            or receipt["content_sha256"] != by_path["content.json"].sha256
            or receipt["files"] != described
            or receipt["validation"]["external_network"] is not False
            or receipt["validation"]["javascript_in_viewer"] is not False
            or receipt["validation"]["approval"] != "not_evaluated"
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise integrity_error(
            "LECTURE_RENDER_INVALID", "render receipt disagrees with its validated outputs"
        ) from exc
    if _read_prefix(root / "review.pdf", 5) != b"%PDF-":
        raise integrity_error("LECTURE_RENDER_INVALID", "lecture PDF signature is missing")
    return [item.model_dump(mode="json") for item in inventory]


def _read_prefix(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def _load_render(root: Path) -> tuple[LectureRequest, RichContent, list[dict[str, Any]]]:
    inventory = _render_inventory(root)
    if _read(root / "render-inventory.json") != pretty_json_bytes(inventory):
        raise integrity_error("LECTURE_RENDER_CHANGED", "rendered lecture files changed")
    request = _read_model(root / "authoring-request.json", LectureRequest)
    content = _read_model(root / "content.json", RichContent)
    _check_content(content, request)
    return request, content, inventory


def import_lecture_source(
    base: Path, edited: Path, request_path: Path, destination: Path, *, editor: str
) -> RichContent:
    if (base / "bundle-manifest.json").is_file():
        manifest = validate_lecture_bundle(base)
        base_version = manifest.version
        base_content = _read_model(base / "content.json", RichContent)
    else:
        old_request, base_content, _inventory = _load_render(base)
        base_version = old_request.version
    request = validate_lecture_request(request_path)
    if request.version <= base_version:
        raise state_error("LECTURE_VERSION_REQUIRED", "edits require a newer lecture version")
    try:
        updated = apply_mdx_edits(
            base_content.model_dump(mode="json"),
            _read(base / "lesson.mdx").decode("utf-8"),
            _read(edited).decode("utf-8"),
        )
        updated["request_fingerprint"] = request.request_fingerprint
        updated["author"] = AgentIdentity(
            role="author",
            provider="source-editor",
            model="portable-mdx",
            version="2.0",
            executor=editor,
            context_id="mdx-edit-" + fingerprint(updated)[:32],
            execution_mode="external_exchange",
            external_api_used=False,
        ).model_dump(mode="json")
        result = RichContent.model_validate(updated)
    except (UnicodeError, ValidationError) as exc:
        raise integrity_error(
            "LECTURE_MDX_INVALID", "MDX edits violate the typed lecture contract"
        ) from exc
    _check_content(result, request)
    destination = _safe_absolute(destination)
    if not destination.parent.is_dir():
        raise configuration_error("LECTURE_PARENT_MISSING", "create the destination parent first")
    atomic_write_new_bytes(destination, pretty_json_bytes(result), private=True)
    return result


def _audit_request(
    request: LectureRequest, content: RichContent, inventory: list[dict[str, Any]], kind: AuditKind
) -> dict[str, Any]:
    checked_figures = [item.id for item in content.figures] if kind == "visual" else []
    checked_equations = [item.id for item in content.equations] if kind == "visual" else []
    checked_blocks = [
        item.id
        for item in content.blocks
        if kind == "content" or item.type in {"equation", "figure", "steps"}
    ]
    criteria = {
        "content": [
            "Re-extract each material claim and verify it against the supplied evidence and original artifacts.",
            "Check derivations, assumptions, teacher emphasis, assignments, follow-on references and coverage gaps.",
            "Confirm current lecture, older lecture and supplementary reasoning are distinguishable.",
        ],
        "visual": [
            "Inspect rendered HTML/PDF and each referenced figure independently against its source and calculations.",
            "Verify axes, units, signs, direction, boundary conditions, support and load geometry, and input data.",
            "Check equations and step calculations, consistency with diagrams, alt text and visual legibility.",
        ],
    }
    result = {
        "schema_version": "2.0",
        "kind": kind,
        "job_id": request.job_id,
        "version": request.version,
        "evidence_bundle_fingerprint": request.evidence_bundle_fingerprint,
        "render_fingerprint": fingerprint(inventory),
        "external_api_allowed": request.external_api_allowed,
        "required_blocks": checked_blocks,
        "required_figures": checked_figures,
        "required_equations": checked_equations,
        "requirements": [item.model_dump(mode="json") for item in request.requirements],
        "ambiguities": [item.model_dump(mode="json") for item in request.ambiguities],
        "evidence": [item.model_dump(mode="json") for item in request.sources],
        "content": content.model_dump(mode="json", exclude={"author", "request_fingerprint"}),
        "review_files": [
            item
            for item in inventory
            if item["path"] not in {"authoring-request.json", "content.json"}
        ],
        "criteria": criteria[kind],
    }
    return {**result, "request_fingerprint": fingerprint(result)}


def prepare_lecture_audit(root: Path, kind: AuditKind) -> dict[str, Any]:
    request, content, inventory = _load_render(root)
    return _audit_request(request, content, inventory, kind)


def _check_audits(
    request: LectureRequest,
    content: RichContent,
    inventory: list[dict[str, Any]],
    audits: tuple[LectureAuditResponse, LectureAuditResponse],
) -> None:
    contexts = {content.author.context_id}
    targets = {
        *[item.id for item in content.blocks],
        *[item.id for item in content.figures],
        *[item.id for item in content.equations],
        *[
            step.id
            for block in content.blocks
            if isinstance(block, StepsBlock)
            for step in block.steps
        ],
        *_TOPICS,
    }
    sources = {item.id for item in content.sources}
    for kind, response in zip(("content", "visual"), audits, strict=True):
        expected = _audit_request(request, content, inventory, kind)
        if response.kind != kind or response.request_fingerprint != expected["request_fingerprint"]:
            raise integrity_error(
                "LECTURE_AUDIT_STALE", "lecture audit does not bind the rendered candidate"
            )
        if response.agent.role != AgentRole.AUDITOR or response.agent.context_id in contexts:
            raise state_error(
                "LECTURE_AUDIT_NOT_INDEPENDENT", "lecture audits require fresh distinct contexts"
            )
        contexts.add(response.agent.context_id)
        if response.agent.external_api_used and not request.external_api_allowed:
            raise state_error(
                "LECTURE_EXTERNAL_API_DENIED", "external lecture audit is unauthorized"
            )
        if any(
            len(actual) != len(set(actual)) or set(actual) != set(expected[name])
            for name, actual in (
                ("required_blocks", response.checked_blocks),
                ("required_figures", response.checked_figures),
                ("required_equations", response.checked_equations),
            )
        ):
            raise integrity_error(
                "LECTURE_AUDIT_INCOMPLETE", "lecture audit did not cover all required targets"
            )
        if any(
            item.target_id not in targets or not set(item.source_refs) <= sources
            for item in response.findings
        ):
            raise integrity_error(
                "LECTURE_AUDIT_REF_INVALID", "audit finding refers to unknown evidence or target"
            )
        if any(item.severity in {AuditSeverity.P0, AuditSeverity.P1} for item in response.findings):
            raise state_error(
                "LECTURE_REVISION_REQUIRED",
                "resolve P0/P1 findings and render a new lecture version",
            )


def _manifest(request: LectureRequest, inventory: list[InventoryFile]) -> RichDeliverableManifest:
    content = next(item for item in inventory if item.path == "content.json")
    values = {
        "schema_version": "2.0",
        "state": "HUMAN_REVIEW_READY",
        "job_id": request.job_id,
        "version": request.version,
        "content_sha256": content.sha256,
        "evidence_bundle_fingerprint": request.evidence_bundle_fingerprint,
        "profile_snapshot_sha256": request.profile_snapshot_sha256,
        "entrypoints": ENTRYPOINTS,
        "inventory": [item.model_dump(mode="json") for item in inventory],
    }
    return RichDeliverableManifest(**values, approval_target_sha256=fingerprint(values))


def publish_lecture_review(
    rendered: Path, content_audit: Path, visual_audit: Path, destination: Path
) -> tuple[RichDeliverableManifest, bool]:
    request, content, render_inventory = _load_render(rendered)
    audits = (
        _read_model(content_audit, LectureAuditResponse),
        _read_model(visual_audit, LectureAuditResponse),
    )
    _check_audits(request, content, render_inventory, audits)
    destination = _safe_absolute(destination)
    if destination.exists():
        manifest = validate_lecture_bundle(destination)
        if (
            _read(destination / "render-inventory.json") == pretty_json_bytes(render_inventory)
            and _read(destination / "audits/content.json") == pretty_json_bytes(audits[0])
            and _read(destination / "audits/visual.json") == pretty_json_bytes(audits[1])
        ):
            return manifest, True
        raise integrity_error(
            "LECTURE_BUNDLE_STALE", "existing review package binds a different candidate"
        )
    with _stage(destination) as stage:
        for item in render_inventory:
            path = item["path"]
            target = stage / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(rendered / path, target)
        atomic_write_json(stage / "render-inventory.json", render_inventory, private=True)
        atomic_write_json(stage / "audits/content.json", audits[0], private=True)
        atomic_write_json(stage / "audits/visual.json", audits[1], private=True)
        atomic_write_json(
            stage / "audit-findings.json",
            {
                audit.kind: [item.model_dump(mode="json") for item in audit.findings]
                for audit in audits
            },
            private=True,
        )
        atomic_write_json(
            stage / "human-review-bundle.json",
            {
                "schema_version": "2.0",
                "job_id": request.job_id,
                "version": request.version,
                "state": "HUMAN_REVIEW_READY",
                "is_approval": False,
                "vep_version": __version__,
                "render_fingerprint": fingerprint(render_inventory),
                "audits": ["audits/content.json", "audits/visual.json"],
                "legacy_draft_sha256": request.legacy_draft_sha256,
            },
            private=True,
        )
        atomic_write_bytes(
            stage / "REVIEW.md",
            (
                b"# Review candidate\n\n"
                b"Read review.pdf or review.html, then inspect audit-findings.json and the sources "
                b"in content.json. Record feedback against the version and approval_target_sha256 "
                b"in bundle-manifest.json. This package is awaiting human review.\n"
            ),
            private=True,
        )
        manifest = _manifest(request, _inventory(stage))
        atomic_write_json(stage / "bundle-manifest.json", manifest, private=True)
        validate_lecture_bundle(stage)
    return manifest, False


def validate_lecture_bundle(root: Path) -> RichDeliverableManifest:
    manifest = _read_model(root / "bundle-manifest.json", RichDeliverableManifest)
    inventory = _inventory(root, exclude={"bundle-manifest.json"})
    if inventory != manifest.inventory:
        raise integrity_error("LECTURE_BUNDLE_CHANGED", "review package inventory does not match")
    raw_inventory = _read(root / "render-inventory.json")
    from_render = [
        item.model_dump(mode="json")
        for item in inventory
        if item.path
        not in {
            "render-inventory.json",
            "audits/content.json",
            "audits/visual.json",
            "audit-findings.json",
            "human-review-bundle.json",
            "REVIEW.md",
        }
    ]
    if raw_inventory != pretty_json_bytes(from_render):
        raise integrity_error(
            "LECTURE_RENDER_CHANGED", "review package render inventory is inconsistent"
        )
    request = _read_model(root / "authoring-request.json", LectureRequest)
    content = _read_model(root / "content.json", RichContent)
    _check_content(content, request)
    _check_audits(
        request,
        content,
        from_render,
        (
            _read_model(root / "audits/content.json", LectureAuditResponse),
            _read_model(root / "audits/visual.json", LectureAuditResponse),
        ),
    )
    if manifest != _manifest(request, inventory):
        raise integrity_error("LECTURE_BUNDLE_CHANGED", "review package lineage does not match")
    return manifest


def approve_lecture_bundle(
    root: Path, *, target_sha256: str, reviewer: str, basis: str, confirm: bool
) -> tuple[LectureApproval, Path, bool]:
    if not confirm:
        raise state_error(
            "LECTURE_APPROVAL_CONFIRMATION_REQUIRED", "human confirmation is required"
        )
    root = _safe_absolute(root)
    manifest = validate_lecture_bundle(root)
    if manifest.approval_target_sha256 != target_sha256:
        raise integrity_error(
            "LECTURE_APPROVAL_TARGET_MISMATCH", "approval target does not match this review package"
        )
    intent = LectureApproval(
        job_id=manifest.job_id,
        version=manifest.version,
        approval_target_sha256=target_sha256,
        reviewer=ReviewerIdentity(identity=reviewer),
        basis=basis,
        approved_at=datetime.now(UTC),
        record_sha256=_ZERO_SHA,
    )
    path = root.with_name(f"{root.name}.approval.json")
    with exclusive_file_lock(
        path.with_name(f".{path.name}.lock"),
        busy_code="LECTURE_APPROVAL_BUSY",
        busy_message="lecture approval receipt is in use",
        failure_code="LECTURE_LOCK_FAILED",
        failure_message="lecture approval lock failed",
    ):
        if path.exists() or path.is_symlink():
            existing = _read_model(path, LectureApproval)
            if existing.record_sha256 != payload_fingerprint(existing, "record_sha256") or (
                existing.model_dump(exclude={"record_sha256", "approved_at"})
                != intent.model_dump(exclude={"record_sha256", "approved_at"})
            ):
                raise integrity_error(
                    "LECTURE_APPROVAL_CONFLICT",
                    "existing approval receipt has a different target or intent",
                )
            return existing, path, True
        approved = intent.model_copy(
            update={"record_sha256": payload_fingerprint(intent, "record_sha256")}
        )
        atomic_write_new_bytes(path, pretty_json_bytes(approved), private=True)
    return approved, path, False


def validate_lecture_approval(root: Path) -> tuple[LectureApproval, Path]:
    root = _safe_absolute(root)
    manifest = validate_lecture_bundle(root)
    path = root.with_name(f"{root.name}.approval.json")
    record = _read_model(path, LectureApproval)
    if (
        record.job_id != manifest.job_id
        or record.version != manifest.version
        or record.approval_target_sha256 != manifest.approval_target_sha256
        or record.record_sha256 != payload_fingerprint(record, "record_sha256")
    ):
        raise integrity_error(
            "LECTURE_APPROVAL_TARGET_MISMATCH",
            "approval receipt does not match this review package",
        )
    return record, path
