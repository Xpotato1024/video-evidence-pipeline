"""Explicit network-enabled provisioning for pinned repository ASR profiles."""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import configuration_error, integrity_error, io_error
from video_evidence_pipeline.storage.atomic import atomic_write_json
from video_evidence_pipeline.storage.layout import is_link_or_junction
from video_evidence_pipeline.storage.permissions import ensure_private_directory
from video_evidence_pipeline.transcription.profiles import (
    AsrModelProvisionManifest,
    model_manifest_path,
    model_storage_directory,
    require_provisioned_model,
    resolve_asr_profile,
)


def _model_identity(root: Path) -> tuple[str, int, int]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files or len(files) > 100_000:
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model file set is invalid")
    hashes: dict[str, str] = {}
    total_size = 0
    for path in files:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model contains a link")
        size = path.stat(follow_symlinks=False).st_size
        if size <= 0:
            raise integrity_error(
                "ASR_MODEL_INVALID", "provisioned ASR model contains an empty file"
            )
        total_size += size
        if total_size > 128 * 1024 * 1024 * 1024:
            raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model is too large")
        hashes[path.relative_to(root).as_posix()] = sha256_file(path)
    return fingerprint(hashes), len(files), total_size


def provision_model(
    repository_root: Path,
    profile_id: str | None = None,
) -> tuple[Path, AsrModelProvisionManifest, bool]:
    selected, profile = resolve_asr_profile(repository_root, profile_id)
    destination = model_storage_directory(repository_root, selected, profile)
    manifest_path = model_manifest_path(repository_root, selected, profile)
    if destination.exists() or manifest_path.exists():
        model, manifest = require_provisioned_model(repository_root, selected, profile)
        identity, count, size = _model_identity(model)
        if (
            identity != manifest.model_storage_identity
            or count != manifest.file_count
            or size != manifest.total_size_bytes
        ):
            raise integrity_error(
                "ASR_MODEL_INVALID", "existing provisioned model does not match its manifest"
            )
        return model, manifest, True

    revision_root = destination.parent
    profile_root = revision_root.parent
    for directory in (
        repository_root / ".local",
        repository_root / ".local" / "models",
        repository_root / ".local" / "models" / "asr",
        profile_root,
    ):
        if directory.exists() and is_link_or_junction(directory):
            raise integrity_error(
                "ASR_MODEL_STORAGE_INVALID", "ASR model storage contains a link or junction"
            )
        ensure_private_directory(directory)
    staging_root = profile_root / f".provision-{uuid.uuid4().hex}"
    staging_model = staging_root / "model"
    ensure_private_directory(staging_root)
    ensure_private_directory(staging_model)
    try:
        try:
            from huggingface_hub import snapshot_download
        except (ImportError, ModuleNotFoundError):
            raise configuration_error(
                "ASR_PROVISION_RUNTIME_UNAVAILABLE",
                "model provisioning requires the repository ASR container runtime",
            ) from None
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        snapshot_download(
            repo_id=profile.model_repository,
            revision=profile.model_revision,
            local_dir=staging_model,
            token=False,
        )
        identity, count, size = _model_identity(staging_model)
        manifest = AsrModelProvisionManifest(
            profile=selected,
            model_repository=profile.model_repository,
            model_revision=profile.model_revision,
            model_storage_identity=identity,
            file_count=count,
            total_size_bytes=size,
            provisioned_at=datetime.now(UTC),
        )
        atomic_write_json(staging_root / "manifest.json", manifest, private=True)
        try:
            staging_root.replace(revision_root)
        except OSError as exc:
            if revision_root.exists():
                shutil.rmtree(staging_root)
                model, existing = require_provisioned_model(repository_root, selected, profile)
                existing_identity, existing_count, existing_size = _model_identity(model)
                if (
                    existing_identity == existing.model_storage_identity
                    and existing_count == existing.file_count
                    and existing_size == existing.total_size_bytes
                ):
                    return model, existing, True
            raise io_error(
                "ASR_MODEL_PROVISION_FAILED", "provisioned model could not be published"
            ) from exc
        return destination, manifest, False
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise
