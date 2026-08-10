"""Canonical local workspace layout and escape prevention."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from video_evidence_pipeline.domain.identifiers import JobId, validate_relative_storage_path
from video_evidence_pipeline.errors import configuration_error, integrity_error, io_error
from video_evidence_pipeline.storage.permissions import ensure_private_directory


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or is_junction()


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
    resolved_target = target.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        common = Path(os.path.commonpath([resolved_target, resolved_root]))
    except ValueError as exc:
        raise integrity_error(
            "WORKSPACE_ESCAPE", "workspace path escapes its allowed root"
        ) from exc
    if common != resolved_root:
        raise integrity_error("WORKSPACE_ESCAPE", "workspace path escapes its allowed root")


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    root: Path
    jobs_root: Path

    @classmethod
    def create(cls, repository_root: Path, job_id: JobId) -> JobWorkspace:
        repository_root = repository_root.absolute()
        jobs_root = repository_root / ".local" / "jobs"
        root = jobs_root / job_id
        _assert_no_links(repository_root, root)
        _assert_resolved_within(root, jobs_root)
        if root.exists():
            raise configuration_error("JOB_EXISTS", "job workspace already exists")
        local_root = repository_root / ".local"
        directories = (
            local_root,
            jobs_root,
            root,
            root / "authorization",
            root / "source",
            root / "source/raw",
            root / "normalized",
            root / "normalized/staging",
            root / "manifests",
            root / "manifests/stages",
            root / "logs",
        )
        for directory in directories:
            ensure_private_directory(directory)
            _assert_no_links(repository_root, directory)
        return cls(root=root, jobs_root=jobs_root)

    @classmethod
    def open(cls, path: Path) -> JobWorkspace:
        root = path.absolute()
        if root.parent.name != "jobs" or root.parent.parent.name != ".local":
            raise configuration_error(
                "WORKSPACE_LAYOUT_INVALID",
                "job workspace must use the .local/jobs/<job-id> layout",
            )
        if not root.is_dir():
            raise io_error("WORKSPACE_NOT_FOUND", "job workspace does not exist")
        jobs_root = root.parent
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
    def transitions_jsonl(self) -> Path:
        return self.safe_path("manifests/transitions.jsonl")
