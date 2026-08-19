"""The only public ASR execution path: pinned Docker image plus explicit mounts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.domain.transcription import TranscriptionConfig
from video_evidence_pipeline.errors import configuration_error, io_error
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.transcription.profiles import resolve_asr_profile

_ASR_CONTRACT_LABEL = "org.xpotato.vep.asr-contract-sha256"


def _docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise configuration_error(
            "ASR_CONTAINER_RUNTIME_UNAVAILABLE",
            "Docker is required for repository-owned ASR execution",
        )
    return executable


def _run(command: list[str], *, code: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=24 * 60 * 60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise io_error(code, str(detail).strip()[:1024] or "ASR container command failed") from None


def _image_digest(docker: str, image: str) -> str | None:
    try:
        completed = subprocess.run(
            [
                docker,
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}|{{.Id}}",
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        repo_digests, separator, image_id = completed.stdout.strip().partition("|")
        if not separator:
            return None
        digests = json.loads(repo_digests)
        if isinstance(digests, list) and digests:
            return digests[0]
        return image_id or None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _container_contract_identity(repository_root: Path) -> str:
    fixed = (
        repository_root / ".dockerignore",
        repository_root / "containers" / "asr" / "Dockerfile",
        repository_root / "containers" / "asr" / "requirements.lock",
        repository_root / "pyproject.toml",
        repository_root / "README.md",
        repository_root / "config" / "asr-profiles.yaml",
    )
    source_files = tuple(sorted((repository_root / "src").rglob("*.py")))
    paths = (*fixed, *source_files)
    if not source_files or any(not path.is_file() for path in paths):
        raise configuration_error(
            "ASR_CONTAINER_CONTRACT_INVALID",
            "repository ASR container contract inputs are incomplete",
        )
    return fingerprint(
        {path.relative_to(repository_root).as_posix(): sha256_file(path) for path in paths}
    )


def _image_contract_label(docker: str, image: str) -> str | None:
    try:
        completed = subprocess.run(
            [
                docker,
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{_ASR_CONTRACT_LABEL}" }}}}',
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return completed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _require_image_identity(docker: str, image: str, repository_root: Path) -> str:
    digest = _image_digest(docker, image)
    expected_contract = _container_contract_identity(repository_root)
    if digest is None or _image_contract_label(docker, digest) != expected_contract:
        raise configuration_error(
            "ASR_CONTAINER_IDENTITY_UNAVAILABLE",
            "ASR image identity or repository build-contract label is missing or stale",
        )
    return digest


def build_asr_image(repository_root: Path, profile_id: str | None = None) -> str:
    docker = _docker()
    _selected, profile = resolve_asr_profile(repository_root, profile_id)
    contract_identity = _container_contract_identity(repository_root)
    _run(
        [
            docker,
            "build",
            "--file",
            str(repository_root / "containers" / "asr" / "Dockerfile"),
            "--tag",
            profile.container_image,
            "--label",
            f"{_ASR_CONTRACT_LABEL}={contract_identity}",
            str(repository_root),
        ],
        code="ASR_CONTAINER_BUILD_FAILED",
    )
    _require_image_identity(docker, profile.container_image, repository_root)
    return profile.container_image


def _gpu_passthrough_usable(docker: str, image: str) -> bool:
    try:
        subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--gpus",
                "all",
                "--entrypoint",
                "python3",
                image,
                "-c",
                (
                    "import ctranslate2; "
                    "count = ctranslate2.get_cuda_device_count(); "
                    "assert count > 0; "
                    "assert any('float16' in ctranslate2.get_supported_compute_types('cuda', i) "
                    "for i in range(count))"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def run_transcription_container(
    workspace_path: Path,
    config: TranscriptionConfig,
) -> str:
    workspace = JobWorkspace.open(workspace_path)
    repository_root = workspace.jobs_root.parent.parent
    selected, profile = resolve_asr_profile(repository_root, config.asr_profile)
    docker = _docker()
    models = repository_root / ".local" / "models"
    models.mkdir(parents=True, exist_ok=True)
    digest = _require_image_identity(docker, profile.container_image, repository_root)
    gpu_usable = _gpu_passthrough_usable(docker, digest)
    if config.asr_device.startswith("cuda") and not gpu_usable:
        raise configuration_error(
            "ASR_DEVICE_UNAVAILABLE", "explicit CUDA is unavailable to the ASR container"
        )
    command = [docker, "run", "--rm", "--pull", "never", "--network", "none"]
    if gpu_usable and config.asr_device != "cpu":
        command.extend(["--gpus", "all"])
    command.extend(
        [
            "--env",
            "VEP_ASR_CONTAINER_INTERNAL=1",
            "--env",
            f"VEP_ASR_CONTAINER_IMAGE_DIGEST={digest or ''}",
            "--mount",
            (f"type=bind,src={workspace.root},dst=/workspace/.local/jobs/{workspace.root.name}"),
            "--mount",
            (f"type=bind,src={models},dst=/workspace/.local/models,readonly"),
            "--mount",
            f"type=bind,src={repository_root / 'config'},dst=/workspace/config,readonly",
            digest,
            "transcribe",
            f"/workspace/.local/jobs/{workspace.root.name}",
            "--asr-profile",
            selected,
            "--asr-device",
            config.asr_device,
            "--minimum-caption-coverage",
            str(config.minimum_caption_coverage),
            "--gap-policy",
            config.gap_policy.value,
            "--overlap-policy",
            config.overlap_policy.value,
            "--json",
        ]
    )
    for language in config.preferred_languages:
        command.extend(["--preferred-language", language])
    if config.asr_language is not None:
        command.extend(["--asr-language", config.asr_language])
    return _run(command, code="ASR_CONTAINER_TRANSCRIPTION_FAILED").stdout.strip()


def run_provision_container(repository_root: Path, profile_id: str | None = None) -> str:
    selected, profile = resolve_asr_profile(repository_root, profile_id)
    docker = _docker()
    digest = _require_image_identity(docker, profile.container_image, repository_root)
    models = repository_root / ".local" / "models"
    models.mkdir(parents=True, exist_ok=True)
    command = [
        docker,
        "run",
        "--rm",
        "--pull",
        "never",
        "--env",
        "VEP_ASR_CONTAINER_INTERNAL=1",
        "--env",
        f"VEP_ASR_CONTAINER_IMAGE_DIGEST={digest}",
        "--mount",
        f"type=bind,src={models},dst=/workspace/.local/models",
        "--mount",
        f"type=bind,src={repository_root / 'config'},dst=/workspace/config,readonly",
        digest,
        "asr",
        "model-provision",
        "--repository-root",
        "/workspace",
        "--profile",
        selected,
        "--json",
    ]
    return _run(command, code="ASR_MODEL_PROVISION_FAILED").stdout.strip()
