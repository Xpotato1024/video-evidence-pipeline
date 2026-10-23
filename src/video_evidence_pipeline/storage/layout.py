"""Canonical local workspace layout and escape prevention."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from video_evidence_pipeline.domain.identifiers import JobId, validate_relative_storage_path
from video_evidence_pipeline.errors import configuration_error, integrity_error, io_error
from video_evidence_pipeline.storage.permissions import (
    PRIVATE_DIRECTORY_MODE,
    enforce_private_directory,
    ensure_private_directory,
)


def is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    if path.is_symlink() or is_junction():
        return True
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_point)


def _is_link_or_junction(path: Path) -> bool:
    return is_link_or_junction(path)


def _assert_no_links(start: Path, target: Path) -> None:
    start = start.absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(start)
    except ValueError as exc:
        raise integrity_error(
            "WORKSPACE_ESCAPE", "workspace path escapes its allowed root"
        ) from exc
    current = start
    for part in (Path() / relative).parts:
        if current.exists() and _is_link_or_junction(current):
            raise integrity_error("WORKSPACE_SYMLINK", "workspace path contains a link or junction")
        current /= part
    if current.exists() and _is_link_or_junction(current):
        raise integrity_error("WORKSPACE_SYMLINK", "workspace path contains a link or junction")


def _assert_resolved_within(target: Path, root: Path) -> None:
    resolved_target = _path_for_commonpath(target.resolve(strict=False))
    resolved_root = _path_for_commonpath(root.resolve(strict=False))
    try:
        common = Path(os.path.commonpath([resolved_target, resolved_root]))
    except ValueError as exc:
        raise integrity_error(
            "WORKSPACE_ESCAPE", "workspace path escapes its allowed root"
        ) from exc
    if common != resolved_root:
        raise integrity_error("WORKSPACE_ESCAPE", "workspace path escapes its allowed root")


def _path_for_commonpath(path: Path) -> Path:
    """Normalize Windows extended paths before comparing resolved roots."""
    value = os.fspath(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("//?/UNC/"):
            value = "//" + value[8:]
        elif value.startswith("\\\\?\\") or value.startswith("//?/"):
            value = value[4:]
    return Path(os.path.normcase(os.path.normpath(value)))


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    root: Path
    jobs_root: Path
    resource_root: Path | None = None

    @property
    def resources_root(self) -> Path:
        return self.resource_root or self.jobs_root.parent.parent

    @classmethod
    def create(
        cls, repository_root: Path, job_id: JobId, *, jobs_root: Path | None = None
    ) -> JobWorkspace:
        from video_evidence_pipeline.runtime_context import current_runtime

        context = current_runtime()
        if context is not None:
            repository_root = context.resources.root
            if jobs_root is not None and jobs_root.resolve() != context.data.jobs_root.resolve():
                raise configuration_error(
                    "WORKSPACE_LAYOUT_INVALID",
                    "installed jobs root must match the runtime data root",
                )
        repository_root = repository_root.absolute()
        jobs_root = jobs_root or (
            context.data.jobs_root if context else repository_root / ".local" / "jobs"
        )
        storage_root = jobs_root.parent
        root = jobs_root / job_id
        _assert_no_links(Path(storage_root.anchor), root)
        _assert_resolved_within(root, jobs_root)
        if context is not None and context.binding_path(root).exists():
            # A failed recovery may have removed only the child it created.
            # Reuse an identical external binding, never overwrite another one.
            context.require_binding(root)
        for directory in (storage_root, jobs_root):
            ensure_private_directory(directory)
            _assert_no_links(Path(storage_root.anchor), directory)
        try:
            root.mkdir(mode=PRIVATE_DIRECTORY_MODE, exist_ok=False)
        except FileExistsError as exc:
            raise configuration_error("JOB_EXISTS", "job workspace already exists") from exc
        except OSError as exc:
            raise io_error(
                "PRIVATE_DIRECTORY_FAILED", "private directory could not be created"
            ) from exc
        enforce_private_directory(root)
        _assert_no_links(storage_root, root)
        directories = (
            root / "authorization",
            root / "source",
            root / "source/raw",
            root / "normalized",
            root / "normalized/staging",
            root / "transcript",
            root / "transcript/staging",
            root / "refinement",
            root / "supporting",
            root / "supporting/staging",
            root / "evidence",
            root / "evidence/staging",
            root / "search",
            root / "search/staging",
            root / "semantic",
            root / "semantic/staging",
            root / "authoring",
            root / "authoring/requests",
            root / "authoring/responses",
            root / "authoring/drafts",
            root / "authoring/audits",
            root / "authoring/revisions",
            root / "authoring/length",
            root / "human-review",
            root / "approval",
            root / "delivery",
            root / "delivery/staging",
            root / "exports",
            root / "exports/markdown",
            root / "manifests",
            root / "manifests/authoring",
            root / "manifests/stages",
            root / "logs",
        )
        for directory in directories:
            ensure_private_directory(directory)
            _assert_no_links(storage_root, directory)
        if context is not None and not context.binding_path(root).exists():
            from video_evidence_pipeline.domain.serialization import canonical_json_bytes
            from video_evidence_pipeline.storage.atomic import atomic_write_new_bytes

            atomic_write_new_bytes(
                context.binding_path(root),
                canonical_json_bytes(context.binding(root)),
                private=True,
            )
        return cls(root=root, jobs_root=jobs_root, resource_root=repository_root)

    @classmethod
    def open(cls, path: Path) -> JobWorkspace:
        from video_evidence_pipeline.runtime_context import current_runtime

        context = current_runtime()
        root = path.absolute()
        if context is None and (root.parent.name != "jobs" or root.parent.parent.name != ".local"):
            raise configuration_error(
                "WORKSPACE_LAYOUT_INVALID",
                "job workspace must use the .local/jobs/<job-id> layout",
            )
        if not root.is_dir():
            raise io_error("WORKSPACE_NOT_FOUND", "job workspace does not exist")
        jobs_root = root.parent
        if context is not None:
            context.require_binding(root)
            if jobs_root.resolve() != context.data.jobs_root.resolve():
                raise configuration_error(
                    "WORKSPACE_LAYOUT_INVALID", "installed job must remain inside its data root"
                )
            _assert_no_links(Path(root.anchor), root)
            _assert_resolved_within(root, context.data.jobs_root)
            return cls(root=root, jobs_root=jobs_root, resource_root=context.resources.root)
        repository_root = jobs_root.parent.parent
        _assert_no_links(repository_root, root)
        _assert_resolved_within(root, jobs_root)
        return cls(root=root, jobs_root=jobs_root)

    def safe_path(self, relative: str) -> Path:
        normalized = validate_relative_storage_path(relative)
        target = self.root.joinpath(*PurePosixPath(normalized).parts)
        _assert_no_links(self.root, target)
        _assert_resolved_within(target, self.root)
        return target

    @property
    def job_yaml(self) -> Path:
        return self.safe_path("job.yaml")

    @property
    def authorization_json(self) -> Path:
        return self.safe_path("authorization/authorization.json")

    @property
    def source_manifest_json(self) -> Path:
        return self.safe_path("manifests/source-manifest.json")

    @property
    def acquisition_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/acquisition.json")

    @property
    def media_manifest_json(self) -> Path:
        return self.safe_path("manifests/media-manifest.json")

    @property
    def normalization_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/normalization.json")

    @property
    def normalization_lock_path(self) -> Path:
        return self.safe_path("normalized/.normalization.lock")

    @property
    def transcript_manifest_json(self) -> Path:
        return self.safe_path("manifests/transcript-manifest.json")

    @property
    def transcript_candidate_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/transcription-candidate.json")

    @property
    def transcription_stage_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/transcription.json")

    @property
    def transcription_lock_path(self) -> Path:
        return self.safe_path("transcript/.transcription.lock")

    @property
    def supporting_manifest_json(self) -> Path:
        return self.safe_path("manifests/supporting-input-manifest.json")

    @property
    def refinement_request_json(self) -> Path:
        return self.safe_path("refinement/request.json")

    @property
    def refinement_manifest_json(self) -> Path:
        return self.safe_path("manifests/transcript-refinement-manifest.json")

    @property
    def refinement_lock_path(self) -> Path:
        return self.safe_path("refinement/.refinement.lock")

    @property
    def evidence_request_json(self) -> Path:
        return self.safe_path("evidence/request.json")

    @property
    def evidence_manifest_json(self) -> Path:
        return self.safe_path("manifests/evidence-manifest.json")

    @property
    def evidence_candidate_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/evidence-candidate.json")

    @property
    def evidence_stage_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/evidence.json")

    @property
    def evidence_lock_path(self) -> Path:
        return self.safe_path("evidence/.evidence.lock")

    @property
    def search_index_sqlite(self) -> Path:
        return self.safe_path("search/index.sqlite3")

    @property
    def search_lock_path(self) -> Path:
        return self.safe_path("search/.search.lock")

    @property
    def semantic_lock_path(self) -> Path:
        return self.safe_path("semantic/.semantic.lock")

    @property
    def human_review_manifest_json(self) -> Path:
        return self.safe_path("manifests/authoring/human-review.json")

    @property
    def approval_records_jsonl(self) -> Path:
        return self.safe_path("approval/records.jsonl")

    @property
    def approval_manifest_json(self) -> Path:
        return self.safe_path("manifests/approval-manifest.json")

    @property
    def approval_stage_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/approval.json")

    @property
    def delivery_staging_root(self) -> Path:
        return self.safe_path("delivery/staging")

    @property
    def export_manifest_json(self) -> Path:
        return self.safe_path("manifests/export-manifest.json")

    @property
    def export_stage_manifest_json(self) -> Path:
        return self.safe_path("manifests/stages/export.json")

    @property
    def recovery_manifest_json(self) -> Path:
        return self.safe_path("manifests/recovery-manifest.json")

    @property
    def transitions_jsonl(self) -> Path:
        return self.safe_path("manifests/transitions.jsonl")
