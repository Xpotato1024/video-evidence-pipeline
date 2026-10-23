"""Allowlisted, byte-preserving study archives; no runtime state is exported."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline import __version__
from video_evidence_pipeline.delivery.study_sources import build_source_index
from video_evidence_pipeline.domain.delivery import ApprovalRecord, approval_record_fingerprint
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.media import MediaManifest
from video_evidence_pipeline.domain.refinement import RefinementManifest
from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.domain.study_bundle import (
    StudyBundleFile,
    StudyBundleManifest,
    StudySourceIndex,
    study_bundle_fingerprint,
)
from video_evidence_pipeline.domain.supporting import SupportingInputManifest
from video_evidence_pipeline.domain.transcription import TranscriptManifest
from video_evidence_pipeline.errors import integrity_error, io_error, state_error
from video_evidence_pipeline.storage.atomic import atomic_copy_exclusive, atomic_write_new_bytes
from video_evidence_pipeline.storage.layout import is_link_or_junction
from video_evidence_pipeline.storage.locking import exclusive_file_lock
from video_evidence_pipeline.storage.permissions import ensure_private_directory

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
OMITTED_PREFIXES = ("source/raw/", "normalized/analysis/", "normalized/audio/")
ALLOWED_PREFIXES = (
    "transcript/segments/",
    "refinement/segments/",
    "refinement/edits/",
    "refinement/ambiguities/",
    "evidence/records/",
    "evidence/ambiguities/",
    "supporting/original/",
    "supporting/text/",
    "supporting/pages/",
    "normalized/frames/",
    "normalized/subtitles/",
    "authoring/drafts/",
    "authoring/audits/",
    "authoring/revisions/",
    "human-review/",
)
STRUCTURED_PREFIXES = (
    "transcript/",
    "refinement/",
    "evidence/",
    "authoring/",
    "human-review/",
    "approval/",
)
MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
}


@dataclass(frozen=True)
class StudyBundleResult:
    manifest: StudyBundleManifest
    destination: Path
    reused: bool


def _safe_absolute(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        if is_link_or_junction(parent):
            raise integrity_error(
                "STUDY_BUNDLE_PATH_INVALID", "archive path contains a link or junction"
            )
    # Normalize Windows short-name aliases after rejecting links, so containment checks agree.
    return path.resolve(strict=False)


def _read(path: Path, maximum: int = MAX_JSON_BYTES) -> bytes:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise integrity_error("STUDY_BUNDLE_INVALID", "archive input is not a regular file")
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except OSError as exc:
        raise io_error("STUDY_BUNDLE_READ_FAILED", "archive input could not be read") from exc
    if len(payload) > maximum:
        raise integrity_error(
            "STUDY_BUNDLE_LIMIT_EXCEEDED", "archive metadata exceeds its size limit"
        )
    return payload


def _refs(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        keys = {
            "relative_storage_path",
            "content_sha256",
            "size_bytes",
            "artifact_id",
            "artifact_type",
        }
        if keys <= value.keys():
            yield StudyBundleFile(
                relative_path=value["relative_storage_path"],
                sha256=value["content_sha256"],
                size_bytes=value["size_bytes"],
            )
        else:
            for child in value.values():
                yield from _refs(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _refs(child)


def _structured_refs(path: Path, relative: str):
    if relative.startswith(STRUCTURED_PREFIXES) and path.suffix in {".json", ".jsonl"}:
        try:
            payload = _read(path).decode("utf-8")
            values = (
                [json.loads(line) for line in payload.splitlines()]
                if path.suffix == ".jsonl"
                else [json.loads(payload)]
            )
            for value in values:
                yield from _refs(value)
        except (UnicodeError, ValueError) as exc:
            raise integrity_error(
                "STUDY_BUNDLE_INVALID", "structured archive artifact is malformed"
            ) from exc


def _allowed(relative: str) -> bool:
    if Path(relative).suffix.lower() in MEDIA_SUFFIXES:
        return False
    if relative == "approval/records.jsonl":
        return True
    if relative.startswith("authoring/"):
        return relative.startswith(ALLOWED_PREFIXES) and Path(relative).name in {
            "draft.md",
            "claims.jsonl",
            "requirement-map.json",
            "findings.json",
            "audit-claims.jsonl",
            "claim-comparison.jsonl",
            "resolutions.jsonl",
        }
    return relative.startswith(ALLOWED_PREFIXES)


def _collect(workspace, fixed, history, human_review, approval):
    transcript = TranscriptManifest.model_validate_json(_read(workspace.transcript_manifest_json))
    media = MediaManifest.model_validate_json(
        _read(workspace.safe_path("manifests/media-manifest.json"))
    )
    seeds = [
        media.raw_artifact,
        transcript.selected_source_artifacts,
        transcript.segments_artifact,
        fixed.evidence_manifest.evidence_artifact,
        fixed.evidence_manifest.ambiguity_artifact,
        fixed.evidence_request.frame_records,
        fixed.evidence_request.approved_artifacts,
        human_review.manifest.bundle_artifact,
        approval.manifest.approval_record_artifact,
    ]
    entrypoints = {
        "summary": "summary.md",
        "transcript_raw": transcript.segments_artifact.relative_storage_path,
        "evidence": fixed.evidence_manifest.evidence_artifact.relative_storage_path,
        "ambiguities": fixed.evidence_manifest.ambiguity_artifact.relative_storage_path,
        "human_review": human_review.manifest.bundle_artifact.relative_storage_path,
        "approval": approval.manifest.approval_record_artifact.relative_storage_path,
    }
    if workspace.refinement_manifest_json.exists():
        refinement = RefinementManifest.model_validate_json(
            _read(workspace.refinement_manifest_json)
        )
        seeds.extend(
            [
                refinement.raw_segments_artifact,
                refinement.refined_segments_artifact,
                refinement.edit_ledger_artifact,
                refinement.ambiguity_artifact,
            ]
        )
        entrypoints["transcript_refined"] = (
            refinement.refined_segments_artifact.relative_storage_path
        )
    if workspace.supporting_manifest_json.exists():
        supporting = SupportingInputManifest.model_validate_json(
            _read(workspace.supporting_manifest_json)
        )
        seeds.append(supporting.inputs)
    for draft, audit in history:
        seeds.extend(
            [
                draft.manifest.draft_artifact,
                draft.manifest.claims_artifact,
                draft.manifest.requirement_map_artifact,
                draft.manifest.resolution_artifact,
            ]
        )
        if audit is not None:
            seeds.extend(
                [
                    audit.manifest.audit_claims_artifact,
                    audit.manifest.findings_artifact,
                    audit.manifest.claim_comparison_artifact,
                ]
            )
    pending = list(_refs(seeds))
    included: dict[str, StudyBundleFile] = {}
    omitted: dict[str, StudyBundleFile] = {}
    while pending:
        reference = pending.pop()
        relative = reference.relative_path
        prior = included.get(relative) or omitted.get(relative)
        if prior is not None:
            if prior != reference:
                raise integrity_error(
                    "STUDY_BUNDLE_REFERENCE_CONFLICT", "one path has conflicting identities"
                )
            continue
        if len(included) + len(omitted) >= 10000:
            raise integrity_error("STUDY_BUNDLE_LIMIT_EXCEEDED", "too many archive references")
        if relative.startswith(OMITTED_PREFIXES):
            omitted[relative] = reference
            continue
        if not _allowed(relative):
            raise integrity_error(
                "STUDY_BUNDLE_REFERENCE_UNSUPPORTED",
                "required artifact is outside the archive allowlist",
            )
        source = workspace.safe_path(relative)
        if (
            not stat.S_ISREG(source.stat().st_mode)
            or source.stat().st_size != reference.size_bytes
            or sha256_file(source) != reference.sha256
        ):
            raise integrity_error(
                "STUDY_BUNDLE_SOURCE_CHANGED", "archive source no longer matches its reference"
            )
        included[relative] = reference
        pending.extend(_structured_refs(source, relative))
    return included, omitted, entrypoints, transcript


def _validate_study_bundle(path: Path) -> StudyBundleManifest:
    """Validate an archive without a job workspace, model, or network."""
    root = _safe_absolute(path)
    manifest_path = _safe_absolute(root / "bundle-manifest.json")
    try:
        payload = _read(manifest_path, MAX_MANIFEST_BYTES)
        manifest = StudyBundleManifest.model_validate_json(payload)
    except (ValueError, ValidationError) as exc:
        raise integrity_error(
            "STUDY_BUNDLE_INVALID", "archive manifest is malformed or inconsistent"
        ) from exc
    if payload != pretty_json_bytes(manifest):
        raise integrity_error("STUDY_BUNDLE_INVALID", "archive manifest is not canonical")
    expected = {item.relative_path: item for item in manifest.files}
    omitted = {item.relative_path: item for item in manifest.omitted_source_artifacts}
    found = set()
    pending = [root]
    entry_count = 0
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            entry_count += 1
            if entry_count > 30000:
                raise integrity_error("STUDY_BUNDLE_LIMIT_EXCEEDED", "archive tree is too large")
            _safe_absolute(child)
            if child.is_dir():
                pending.append(child)
            elif child.is_file():
                found.add(child.relative_to(root).as_posix())
            else:
                raise integrity_error(
                    "STUDY_BUNDLE_INVALID", "archive contains a non-regular entry"
                )
    if found != {*expected, "bundle-manifest.json"}:
        raise integrity_error("STUDY_BUNDLE_INVALID", "archive has missing or unexpected files")
    generated = {
        "summary.md",
        "provenance.json",
        "source-index.json",
        "README.md",
        ".gitattributes",
    }
    for relative, reference in expected.items():
        if relative not in generated and not _allowed(relative):
            raise integrity_error(
                "STUDY_BUNDLE_INVALID", "archive contains an unsupported artifact"
            )
        file_path = root / relative
        if (
            file_path.stat().st_size != reference.size_bytes
            or sha256_file(file_path) != reference.sha256
        ):
            raise integrity_error("STUDY_BUNDLE_INVALID", "archive file identity does not match")
        for dependency in _structured_refs(file_path, relative):
            if (
                expected.get(dependency.relative_path) or omitted.get(dependency.relative_path)
            ) != dependency:
                raise integrity_error(
                    "STUDY_BUNDLE_REFERENCE_MISSING", "archive reference cannot be resolved"
                )
    index = StudySourceIndex.model_validate_json(_read(root / manifest.entrypoints["source_index"]))
    if index != build_source_index(root, expected):
        raise integrity_error(
            "STUDY_BUNDLE_SOURCE_REF_MISSING",
            "semantic source index does not match archive payloads",
        )
    records = _read(root / manifest.entrypoints["approval"]).splitlines()
    if len(records) != 1:
        raise integrity_error(
            "STUDY_BUNDLE_INVALID", "archive approval ledger must contain one record"
        )
    record = ApprovalRecord.model_validate_json(records[0])
    if (
        record.record_sha256 != manifest.approval_record_sha256
        or record.record_sha256 != approval_record_fingerprint(record.model_dump(mode="json"))
        or record.job_id != manifest.job_id
        or record.job_fingerprint != manifest.source_job_fingerprint
        or record.approved_draft_artifact.content_sha256 != manifest.approved_draft_sha256
        or record.approved_at != manifest.approved_at
    ):
        raise integrity_error("STUDY_BUNDLE_INVALID", "archive approval binding is inconsistent")
    return manifest


def _file_entry(relative: str, payload: bytes) -> StudyBundleFile:
    return StudyBundleFile(
        relative_path=relative, sha256=sha256_bytes(payload), size_bytes=len(payload)
    )


def _copy_verified(source: Path, target: Path, entry: StudyBundleFile) -> None:
    def verify(path: Path) -> None:
        if path.stat().st_size != entry.size_bytes or sha256_file(path) != entry.sha256:
            raise integrity_error(
                "STUDY_BUNDLE_SOURCE_CHANGED", "archive bytes changed during publication"
            )

    atomic_copy_exclusive(source, target, verifier=verify)


def publish_study_bundle(workspace, job, fixed, history, human_review, approval, destination):
    """Publish a validated archival view, not a mutable VEP workspace."""
    destination = _safe_absolute(destination)
    from video_evidence_pipeline.runtime_context import current_runtime

    context = current_runtime()
    protected_roots = (
        workspace.resources_root.resolve(),
        workspace.jobs_root.parent.resolve(),
        *((context.data.bindings_root.resolve(), *context.protected_roots) if context else ()),
    )
    if any(
        destination.is_relative_to(root) or root.is_relative_to(destination)
        for root in (item.resolve() for item in protected_roots)
    ):
        raise state_error(
            "STUDY_BUNDLE_DESTINATION_INVALID", "choose a destination outside the VEP repository"
        )
    if not destination.parent.is_dir():
        raise state_error(
            "STUDY_BUNDLE_DESTINATION_INVALID", "destination parent must already exist"
        )
    included, omitted, entrypoints, transcript = _collect(
        workspace, fixed, history, human_review, approval
    )
    approved = approval.record.approved_draft_artifact
    included["summary.md"] = StudyBundleFile(
        relative_path="summary.md", sha256=approved.content_sha256, size_bytes=approved.size_bytes
    )
    source_index = build_source_index(workspace.root, included)
    entrypoints["source_index"] = "source-index.json"
    provenance = {
        "schema_version": "1.0",
        "job_id": job.job_id,
        "assignment": job.assignment.model_dump(mode="json"),
        "fixed_evidence_fingerprint": fixed.fixed_evidence_fingerprint,
        "raw_transcript_fingerprint": transcript.transcription_fingerprint,
        "author": history[-1][0].manifest.producer.model_dump(mode="json"),
        "evidence_analyzer": fixed.evidence_manifest.analyzer.model_dump(mode="json"),
        "audit": history[-1][1].manifest.auditor.model_dump(mode="json"),
        "raw_media_available_in_bundle": False,
        "privacy_note": "Source content is preserved, not automatically redacted. Review before sharing.",
    }
    generated = {
        "provenance.json": pretty_json_bytes(provenance),
        "source-index.json": pretty_json_bytes(source_index),
        ".gitattributes": b"* -text\n",
        "README.md": (
            b"# Portable study bundle\n\nStart with [summary.md](summary.md).\n"
            b"See bundle-manifest.json entrypoints for transcripts, evidence and review.\n"
            b"Artifact references are relative to this directory. Input A/V is intentionally omitted.\n"
            b"This archive is not a resumable VEP workspace or an authenticated signature.\n"
        ),
    }
    included.update({name: _file_entry(name, payload) for name, payload in generated.items()})
    values = {
        "schema_version": "1.0",
        "format": "study-bundle",
        "job_id": job.job_id,
        "source_job_fingerprint": approval.record.job_fingerprint,
        "vep_version": __version__,
        "approval_record_sha256": approval.record.record_sha256,
        "approved_draft_sha256": approved.content_sha256,
        "approved_at": approval.record.approved_at.isoformat().replace("+00:00", "Z"),
        "approval_identity_cryptographically_verified": False,
        "input_media_included": False,
        "runtime_replay_supported": False,
        "entrypoints": entrypoints,
        "files": [included[key].model_dump(mode="json") for key in sorted(included)],
        "omitted_source_artifacts": [
            omitted[key].model_dump(mode="json") for key in sorted(omitted)
        ],
    }
    manifest = StudyBundleManifest(**values, bundle_fingerprint=study_bundle_fingerprint(values))
    lock_path = destination.parent / f".{destination.name}.vep-export.lock"
    _safe_absolute(lock_path)
    with exclusive_file_lock(
        lock_path,
        busy_code="STUDY_BUNDLE_BUSY",
        busy_message="archive destination is in use",
        failure_code="STUDY_BUNDLE_LOCK_FAILED",
        failure_message="archive destination could not be locked",
    ):
        _safe_absolute(destination)
        if destination.exists():
            stored = validate_study_bundle(destination)
            if stored != manifest:
                raise state_error(
                    "STUDY_BUNDLE_DESTINATION_CONFLICT", "existing archive has different contents"
                )
            return StudyBundleResult(stored, destination, True)
        staging = Path(tempfile.mkdtemp(prefix=".vep-study-", dir=destination.parent))
        try:
            for relative, entry in included.items():
                parent = staging
                for part in Path(relative).parts[:-1]:
                    parent = parent / part
                    ensure_private_directory(parent)
                if relative in generated:
                    atomic_write_new_bytes(staging / relative, generated[relative], private=True)
                else:
                    source_relative = (
                        approved.relative_storage_path if relative == "summary.md" else relative
                    )
                    _copy_verified(workspace.safe_path(source_relative), staging / relative, entry)
            atomic_write_new_bytes(
                staging / "bundle-manifest.json", pretty_json_bytes(manifest), private=True
            )
            validate_study_bundle(staging)
            # Reserve exclusively: never replace even an empty caller-owned directory.
            destination.mkdir(mode=0o700, exist_ok=False)
            for child in staging.iterdir():
                if child.name != "bundle-manifest.json":
                    child.rename(destination / child.name)
            # Completion marker is published last. Interrupted destinations fail validation.
            atomic_write_new_bytes(
                destination / "bundle-manifest.json", pretty_json_bytes(manifest), private=True
            )
            validate_study_bundle(destination)
        except OSError as exc:
            raise io_error(
                "STUDY_BUNDLE_WRITE_FAILED",
                "archive publication failed; existing contents were not replaced",
            ) from exc
        finally:
            shutil.rmtree(staging)
    return StudyBundleResult(manifest, destination, False)


def validate_study_bundle(path: Path) -> StudyBundleManifest:
    """Return a checked inventory; reject partial, unsafe and changed archives."""
    try:
        return _validate_study_bundle(path)
    except (ValueError, UnicodeError) as exc:
        raise integrity_error("STUDY_BUNDLE_INVALID", "archive metadata is malformed") from exc
    except OSError as exc:
        raise io_error("STUDY_BUNDLE_READ_FAILED", "archive could not be inspected") from exc
