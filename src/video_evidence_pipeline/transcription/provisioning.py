"""Explicit network-enabled provisioning for pinned repository ASR profiles."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import configuration_error, integrity_error
from video_evidence_pipeline.runtime_context import model_root
from video_evidence_pipeline.storage.atomic import atomic_write_json
from video_evidence_pipeline.storage.layout import is_link_or_junction
from video_evidence_pipeline.storage.locking import exclusive_file_lock
from video_evidence_pipeline.storage.permissions import ensure_private_directory
from video_evidence_pipeline.transcription.profiles import (
    AsrModelProvisionManifest,
    AsrProfile,
    model_manifest_path,
    model_storage_directory,
    require_provisioned_model,
    resolve_asr_profile,
    validate_model_runtime_contract,
)

_CONTROL_METADATA = Path(".cache/huggingface")
_STALE_STAGING_NAME = re.compile(r"^\.provision-[0-9a-f]{32}$")


def _is_control_metadata(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    return relative.parts[:2] == _CONTROL_METADATA.parts


def model_payload_hashes(root: Path, profile=None) -> dict[str, str]:
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    if any(is_link_or_junction(path) for path in entries):
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model contains a link")
    files = [path for path in entries if path.is_file() and not _is_control_metadata(root, path)]
    if not files or len(files) > 100_000:
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model file set is invalid")
    validate_model_runtime_contract(root, profile)
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


def model_payload_identity(root: Path, profile=None) -> tuple[str, int, int]:
    hashes = model_payload_hashes(root, profile)
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
    # The host validates the baked profile-specific contract before publication.


def publish_model_payload(
    repository_root: Path,
    profile_id: str | None,
    staged_model: Path,
    *,
    expected_profile: AsrProfile | None = None,
) -> tuple[Path, AsrModelProvisionManifest, bool]:
    """Validate worker bytes and publish them with host-native storage semantics."""

    selected, profile = resolve_asr_profile(repository_root, profile_id)
    if expected_profile is not None and (selected != profile_id or profile != expected_profile):
        raise configuration_error(
            "ASR_PROFILE_CHANGED", "ASR profile changed while model provisioning was running"
        )
    destination = model_storage_directory(repository_root, selected, profile)
    manifest_path = model_manifest_path(repository_root, selected, profile)
    if destination.exists() or manifest_path.exists():
        model, manifest = require_provisioned_model(repository_root, selected, profile)
        identity, count, size = model_payload_identity(model, profile)
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
    identity, count, size = model_payload_identity(staged_model, profile)

    revision_root = destination.parent
    profile_root = revision_root.parent
    for directory in (
        model_root(repository_root).parent,
        model_root(repository_root),
        model_root(repository_root) / "asr",
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
            staging_root / "model", profile
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


def _profile_root(repository_root: Path, profile_id: str) -> Path:
    return model_root(repository_root) / "asr" / profile_id


def _ensure_profile_storage(repository_root: Path, profile_id: str) -> Path:
    profile_root = _profile_root(repository_root, profile_id)
    for directory in (
        model_root(repository_root).parent,
        model_root(repository_root),
        model_root(repository_root) / "asr",
        profile_root,
    ):
        if directory.exists() and is_link_or_junction(directory):
            raise integrity_error(
                "ASR_MODEL_STORAGE_INVALID", "ASR model storage contains a link or junction"
            )
        ensure_private_directory(directory)
    return profile_root


@contextmanager
def provision_lock(repository_root: Path, profile_id: str, revision: str) -> Iterator[None]:
    """Serialize profile storage, then bind the lifecycle to one immutable revision."""

    profile_root = _ensure_profile_storage(repository_root, profile_id)
    with (
        exclusive_file_lock(
            profile_root / ".provision.lock",
            busy_code="ASR_MODEL_PROVISION_BUSY",
            busy_message="ASR model provisioning is already running for this profile",
            failure_code="ASR_MODEL_PROVISION_LOCK_FAILED",
            failure_message="ASR model provisioning could not be locked",
            timeout_seconds=24 * 60 * 60,
        ),
        exclusive_file_lock(
            profile_root / f".provision-{revision}.lock",
            busy_code="ASR_MODEL_PROVISION_BUSY",
            busy_message="ASR model provisioning is already running for this profile revision",
            failure_code="ASR_MODEL_PROVISION_LOCK_FAILED",
            failure_message="ASR model provisioning could not be revision-locked",
            timeout_seconds=24 * 60 * 60,
        ),
    ):
        yield


def cleanup_stale_provision_staging(repository_root: Path, profile_id: str) -> None:
    """Remove only known direct-child publication staging directories while locked."""

    profile_root = _profile_root(repository_root, profile_id)
    for candidate in profile_root.iterdir():
        if not _STALE_STAGING_NAME.fullmatch(candidate.name):
            continue
        if is_link_or_junction(candidate) or not candidate.is_dir():
            raise integrity_error(
                "ASR_MODEL_STORAGE_INVALID", "stale ASR provision staging is unsafe"
            )
        shutil.rmtree(candidate)


def existing_provisioned_model(
    repository_root: Path, profile_id: str, profile
) -> tuple[Path, AsrModelProvisionManifest, bool] | None:
    """Return a fully validated existing model or fail closed on partial state."""

    destination = model_storage_directory(repository_root, profile_id, profile)
    manifest_path = model_manifest_path(repository_root, profile_id, profile)
    if not destination.exists() and not manifest_path.exists():
        return None
    model, manifest = require_provisioned_model(repository_root, profile_id, profile)
    identity, count, size = model_payload_identity(model, profile)
    if (
        identity != manifest.model_storage_identity
        or count != manifest.file_count
        or size != manifest.total_size_bytes
    ):
        raise integrity_error(
            "ASR_MODEL_INVALID", "existing provisioned model does not match its manifest"
        )
    return model, manifest, True


def provision_model(
    repository_root: Path,
    profile_id: str | None = None,
) -> tuple[Path, AsrModelProvisionManifest, bool]:
    selected, profile = resolve_asr_profile(repository_root, profile_id)
    with provision_lock(repository_root, selected, profile.model_revision):
        cleanup_stale_provision_staging(repository_root, selected)
        existing = existing_provisioned_model(repository_root, selected, profile)
        if existing is not None:
            return existing
        with tempfile.TemporaryDirectory(prefix="vep-asr-provision-") as temporary:
            staging_model = Path(temporary) / "model"
            download_model_payload(
                staging_model,
                profile.model_repository,
                profile.model_revision,
            )
            return publish_model_payload(
                repository_root,
                selected,
                staging_model,
                expected_profile=profile,
            )
