"""Repository-owned ASR profiles and provisioned-model identity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import Sha256
from video_evidence_pipeline.errors import configuration_error, integrity_error
from video_evidence_pipeline.runtime_context import model_root, resource_root

DEFAULT_ASR_PROFILE = "whisper-large-v3"
PROFILE_REGISTRY_PATH = Path("config/asr-profiles.yaml")
WHISPER_LARGE_V3_REQUIRED_FILES = (
    "config.json",
    "model.bin",
    "preprocessor_config.json",
    "tokenizer.json",
)
LEGACY_LOCAL_MODEL_REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")


class AsrProfile(StrictModel):
    engine: Literal["faster-whisper"]
    engine_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    runtime: Literal["CTranslate2"]
    runtime_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    model: Literal["large-v3"]
    model_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    gpu_compute_type: Literal["float16"] = "float16"
    cpu_compute_type: Literal["int8"] = "int8"
    min_free_vram_mib: int = Field(ge=1, le=1_048_576)
    default_device_policy: Literal["auto"] = "auto"
    container_image: str = Field(min_length=1, max_length=512)


class AsrProfileRegistry(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    default_profile: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    profiles: dict[str, AsrProfile] = Field(min_length=1, max_length=32)

    @field_validator("profiles")
    @classmethod
    def profile_ids_are_canonical(cls, value: dict[str, AsrProfile]) -> dict[str, AsrProfile]:
        if list(value) != sorted(value) or any(
            not key
            or len(key) > 128
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in key)
            for key in value
        ):
            raise ValueError("ASR profile IDs must be sorted lowercase hyphenated names")
        return value

    @model_validator(mode="after")
    def default_exists(self) -> Self:
        if self.default_profile not in self.profiles:
            raise ValueError("default ASR profile is missing")
        return self


class AsrModelProvisionManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    profile: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    model_repository: str
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    model_storage_identity: Sha256
    file_count: int = Field(ge=1, le=100_000)
    total_size_bytes: int = Field(ge=1, le=128 * 1024 * 1024 * 1024)
    provisioned_at: UtcDateTime


def load_asr_profile_registry(repository_root: Path) -> AsrProfileRegistry:
    path = resource_root(repository_root) / PROFILE_REGISTRY_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return AsrProfileRegistry.model_validate(data)
    except FileNotFoundError:
        raise configuration_error(
            "ASR_PROFILE_REGISTRY_MISSING", "repository ASR profile registry is missing"
        ) from None
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError):
        raise configuration_error(
            "ASR_PROFILE_REGISTRY_INVALID", "repository ASR profile registry is invalid"
        ) from None


def resolve_asr_profile(repository_root: Path, profile_id: str | None) -> tuple[str, AsrProfile]:
    registry = load_asr_profile_registry(repository_root)
    selected = profile_id or registry.default_profile
    try:
        return selected, registry.profiles[selected]
    except KeyError:
        raise configuration_error(
            "ASR_PROFILE_UNKNOWN", "ASR profile is not registered in the repository"
        ) from None


def required_model_files(profile: AsrProfile | None) -> tuple[str, ...]:
    """Return the runtime-essential file contract for the repository profile."""

    if profile is None:
        return LEGACY_LOCAL_MODEL_REQUIRED_FILES
    if profile.model != "large-v3":  # pragma: no cover - schema currently fixes this profile
        raise configuration_error("ASR_PROFILE_INVALID", "unsupported ASR model profile")
    return WHISPER_LARGE_V3_REQUIRED_FILES


def validate_model_runtime_contract(root: Path, profile: AsrProfile | None) -> None:
    """Validate profile-critical model metadata before provisioning or inference."""

    if any(not (root / name).is_file() for name in required_model_files(profile)):
        raise integrity_error("ASR_MODEL_INVALID", "provisioned ASR model is incomplete")
    if profile is None:
        return
    try:
        preprocessor = json.loads((root / "preprocessor_config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise integrity_error(
            "ASR_MODEL_INVALID", "ASR model preprocessor configuration is invalid"
        ) from None
    if not isinstance(preprocessor, dict) or preprocessor.get("feature_size") != 128:
        raise integrity_error(
            "ASR_MODEL_INVALID", "large-v3 requires preprocessor feature_size 128"
        )


def model_storage_directory(repository_root: Path, profile_id: str, profile: AsrProfile) -> Path:
    return model_root(repository_root) / "asr" / profile_id / profile.model_revision / "model"


def model_manifest_path(repository_root: Path, profile_id: str, profile: AsrProfile) -> Path:
    return model_storage_directory(repository_root, profile_id, profile).parent / "manifest.json"


def require_provisioned_model(
    repository_root: Path,
    profile_id: str,
    profile: AsrProfile,
) -> tuple[Path, AsrModelProvisionManifest]:
    from video_evidence_pipeline.storage.atomic import read_json_model

    model_directory = model_storage_directory(repository_root, profile_id, profile)
    manifest = read_json_model(
        model_manifest_path(repository_root, profile_id, profile),
        AsrModelProvisionManifest,
        code="ASR_MODEL_NOT_PROVISIONED",
    )
    if (
        not model_directory.is_dir()
        or model_directory.is_symlink()
        or manifest.profile != profile_id
        or manifest.model_repository != profile.model_repository
        or manifest.model_revision != profile.model_revision
    ):
        raise integrity_error(
            "ASR_MODEL_NOT_PROVISIONED",
            "required model is not provisioned for the selected repository profile",
        )
    return model_directory, manifest
