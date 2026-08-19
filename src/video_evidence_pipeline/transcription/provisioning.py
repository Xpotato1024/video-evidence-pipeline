"""Explicit network-enabled provisioning for pinned repository ASR profiles."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import configuration_error, integrity_error
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

_CONTROL_METADATA = Path(".cache/huggingface")
_REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")


def _is_control_metadata(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    return relative.parts[:2] == _CONTROL_METADATA.parts


def model_payload_hashes(root: Path) -> dict[str, str]:
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    if any(is_link_or_junction(path) for path in entries):
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model contains a link")
    files = [path for path in entries if path.is_file() and not _is_control_metadata(root, path)]
    if not files or len(files) > 100_000:
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model file set is invalid")
    if any(not (root / name).is_file() for name in _REQUIRED_MODEL_FILES):
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model is incomplete")
    hashes: dict[str, str] = {}
    total_size = 0
    for path in files:
        size = path.stat(follow_symlinks=False).st_size
        if size <= 0:
            raise integrity_error(
                "ASR_MODEL_INVALID", "provisioned ASR model contains an empty file"
            )
        total_size += size
        if total_size > 128 * 1024 * 1024 * 1024:
            raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model is too large")
        hashes[path.relative_to(root).as_posix()] = sha256_file(path)
    return hashes


def model_payload_identity(root: Path) -> tuple[str, int, int]:
    hashes = model_payload_hashes(root)
    total_size = sum((root / relative).stat(follow_symlinks=False).st_size for relative in hashes)
    return fingerprint(hashes), len(hashes), total_size


def _remove_download_control_metadata(root: Path) -> None:
    control = root / _CONTROL_METADATA
    if not control.exists():
        return
    if is_link_or_junction(control):
        raise integrity_error("ASR_MODEL_INVALID", "download control metadata is a link")
    shutil.rmtree(control)
    cache = root / ".cache"
    if cache.exists() and not any(cache.iterdir()):
        cache.rmdir()


def download_model_payload(destination: Path, repository: str, revision: str) -> None:
    """Network-enabled worker operation; it never receives canonical model storage."""

    if destination.exists() or is_link_or_junction(destination.parent):
        raise integrity_error("ASR_MODEL_STORAGE_INVALID", "ASR download staging is invalid")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        from huggingface_hub import snapshot_download
    except (ImportError, ModuleNotFoundError):
        raise configuration_error(
            "ASR_PROVISION_RUNTIME_UNAVAILABLE",
            "model provisioning requires the repository ASR container runtime",
        ) from None
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    snapshot_download(repo_id=repository, revision=revision, local_dir=destination, token=False)
    _remove_download_control_metadata(destination)
    model_payload_identity(destination)


def publish_model_payload(
    repository_root: Path,
    profile_id: str | None,
    staged_model: Path,
) -> tuple[Path, AsrModelProvisionManifest, bool]:
    """Validate worker bytes and publish them with host-native storage semantics."""

    selected, profile = resolve_asr_profile(repository_root, profile_id)
    destination = model_storage_directory(repository_root, selected, profile)
    manifest_path = model_manifest_path(repository_root, selected, profile)
    if destination.exists() or manifest_path.exists():
        model, manifest = require_provisioned_model(repository_root, selected, profile)
        identity, count, size = model_payload_identity(model)
        if (
            identity != manifest.model_storage_identity
            or count != manifest.file_count
            or size != manifest.total_size_bytes
        ):
            raise integrity_error(
                "ASR_MODEL_INVALID", "existing provisioned model does not match its manifest"
            )
        return model, manifest, True
    if not staged_model.is_dir() or is_link_or_junction(staged_model):
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR staging payload is invalid")
    _remove_download_control_metadata(staged_model)
    identity, count, size = model_payload_identity(staged_model)

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
    ensure_private_directory(staging_root)
    try:
        shutil.copytree(staged_model, staging_root / "model")
        published_identity, published_count, published_size = model_payload_identity(
            staging_root / "model"
        )
        if (published_identity, published_count, published_size) != (identity, count, size):
            raise integrity_error("ASR_MODEL_INVALID", "ASR model changed during host publication")
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
        staging_root.replace(revision_root)
        return destination, manifest, False
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise


def provision_model(
    repository_root: Path,
    profile_id: str | None = None,
) -> tuple[Path, AsrModelProvisionManifest, bool]:
    selected, profile = resolve_asr_profile(repository_root, profile_id)
    destination = model_storage_directory(repository_root, selected, profile)
    manifest_path = model_manifest_path(repository_root, selected, profile)
    if destination.exists() or manifest_path.exists():
        return publish_model_payload(repository_root, selected, destination)
    with tempfile.TemporaryDirectory(prefix="vep-asr-provision-") as temporary:
        staging_model = Path(temporary) / "model"
        download_model_payload(
            staging_model,
            profile.model_repository,
            profile.model_revision,
        )
        return publish_model_payload(repository_root, selected, staging_model)
