"""Host-owned Docker adapters for the narrow ASR worker and provisioning worker."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.serialization import fingerprint, pretty_json_bytes
from video_evidence_pipeline.domain.transcription import (
    MAXIMUM_ASR_WORKER_REQUEST_BYTES,
    AsrSegment,
    AsrWorkerRequest,
    AsrWorkerResponse,
    DeviceSelectionReason,
    TranscriptInterval,
    TranscriptionBackendIdentity,
    TranscriptionConfig,
)
from video_evidence_pipeline.errors import VepError, configuration_error, integrity_error, io_error
from video_evidence_pipeline.transcription.device import is_gpu_resource_error
from video_evidence_pipeline.transcription.profiles import (
    require_provisioned_model,
    resolve_asr_profile,
)
from video_evidence_pipeline.transcription.provisioning import (
    cleanup_stale_provision_staging,
    existing_provisioned_model,
    provision_lock,
)

_ASR_CONTRACT_LABEL = "org.xpotato.vep.asr-contract-sha256"
_MAXIMUM_WORKER_OUTPUT_BYTES = 128 * 1024 * 1024
_MAXIMUM_WORKER_ERROR_BYTES = 1024 * 1024
_DEFAULT_PROCESS_TIMEOUT_SECONDS = 24 * 60 * 60
_DOCKER_CLEANUP_TIMEOUT_SECONDS = 10
_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _BoundedProcessFailure(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise configuration_error(
            "ASR_CONTAINER_RUNTIME_UNAVAILABLE",
            "Docker is required for repository-owned ASR execution",
        )
    return executable


def _run_bounded_process(
    command: list[str],
    *,
    timeout_seconds: float = _DEFAULT_PROCESS_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = _MAXIMUM_WORKER_OUTPUT_BYTES,
    stderr_limit_bytes: int = _MAXIMUM_WORKER_ERROR_BYTES,
) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise _BoundedProcessFailure("start_failure", str(exc)) from None
        if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
            raise _BoundedProcessFailure("start_failure", "ASR container pipes are unavailable")
        failure_lock = threading.Lock()
        failures: list[_BoundedProcessFailure] = []
        stdout = BytesIO()
        stderr = BytesIO()

        def fail(kind: str, detail: str) -> None:
            with failure_lock:
                if not failures:
                    failures.append(_BoundedProcessFailure(kind, detail))
            with suppress(OSError):
                process.kill()

        def drain(stream, destination: BytesIO, limit: int, stream_name: str) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    remaining = limit - destination.tell()
                    if remaining > 0:
                        destination.write(chunk[:remaining])
                    if len(chunk) > remaining:
                        fail(
                            f"{stream_name}_overflow",
                            f"ASR container {stream_name} exceeded its byte limit",
                        )
                        return
            except Exception as exc:
                fail("reader_failure", f"ASR container output reader failed: {exc}")

        readers = (
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, stdout_limit_bytes, "stdout"),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, stderr_limit_bytes, "stderr"),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            fail("timeout", "ASR container command timed out")
            with suppress(OSError, subprocess.SubprocessError):
                process.wait(timeout=30)
        finally:
            for reader in readers:
                reader.join(timeout=30)
        if any(reader.is_alive() for reader in readers):
            fail("reader_failure", "ASR container output reader did not stop")
        if failures:
            raise failures[0]
        try:
            stdout_text = stdout.getvalue().decode("utf-8")
            stderr_text = stderr.getvalue().decode("utf-8")
        except UnicodeDecodeError:
            raise _BoundedProcessFailure(
                "decode_failure", "ASR container output is not valid UTF-8"
            ) from None
        return subprocess.CompletedProcess(command, returncode, stdout_text, stderr_text)
    except _BoundedProcessFailure:
        raise
    except Exception as exc:
        raise _BoundedProcessFailure(
            "host_failure", f"ASR container host process failed: {exc}"
        ) from exc
    finally:
        if process is not None and process.poll() is None:
            with suppress(OSError):
                process.kill()


def _process_failure_error(
    code: str,
    command: list[str],
    failure: _BoundedProcessFailure | subprocess.CalledProcessError,
) -> VepError:
    if isinstance(failure, subprocess.CalledProcessError):
        detail = failure.stderr or failure.output or str(failure)
    else:
        detail = failure.detail
    return io_error(code, str(detail).strip()[:1024] or "ASR container command failed")


def _run(
    command: list[str],
    *,
    code: str,
    timeout_seconds: float = _DEFAULT_PROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = _run_bounded_process(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=_MAXIMUM_WORKER_OUTPUT_BYTES,
            stderr_limit_bytes=_MAXIMUM_WORKER_ERROR_BYTES,
        )
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode,
                command,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return completed
    except (_BoundedProcessFailure, subprocess.CalledProcessError) as exc:
        raise _process_failure_error(code, command, exc) from None


def _docker_cleanup_error(message: str) -> VepError:
    return io_error("ASR_CONTAINER_CLEANUP_FAILED", message)


def _docker_cleanup_command(docker: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [docker, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_DOCKER_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _docker_cleanup_error(f"Docker container cleanup command failed: {exc}") from None


def _is_exact_container_not_found(
    completed: subprocess.CompletedProcess[str], container_id: str
) -> bool:
    if completed.returncode == 0:
        return False
    detail = completed.stderr.strip()
    escaped = re.escape(container_id)
    return any(
        re.fullmatch(pattern, detail) is not None
        for pattern in (
            rf"(?:(?:Error response from daemon: )|Error: )?No such container: {escaped}",
            rf"Error: No such object: {escaped}",
        )
    )


def _read_managed_container_id(cid_path: Path, *, required: bool) -> str | None:
    if not cid_path.exists():
        if required:
            raise _docker_cleanup_error(
                "Docker did not publish the managed container ID after a bounded failure"
            )
        return None
    if cid_path.is_symlink() or not cid_path.is_file() or cid_path.stat().st_size > 65:
        raise _docker_cleanup_error("Docker managed container ID file is invalid")
    try:
        container_id = cid_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise _docker_cleanup_error("Docker managed container ID file is unreadable") from None
    if _CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
        raise _docker_cleanup_error("Docker managed container ID is malformed")
    return container_id


def _terminate_managed_docker_container(
    docker: str, cid_path: Path, *, identity_required: bool
) -> None:
    container_id = _read_managed_container_id(cid_path, required=identity_required)
    if container_id is None:
        return
    inspected = _docker_cleanup_command(
        docker, ["container", "inspect", "--format", "{{.State.Running}}", container_id]
    )
    if _is_exact_container_not_found(inspected, container_id):
        return
    if inspected.returncode != 0:
        raise _docker_cleanup_error(
            "Docker could not inspect the exact managed container during cleanup"
        )
    running = inspected.stdout.strip().lower()
    if running not in {"true", "false"}:
        raise _docker_cleanup_error("Docker returned an invalid managed container state")
    if running == "true":
        killed = _docker_cleanup_command(docker, ["container", "kill", container_id])
        if _is_exact_container_not_found(killed, container_id):
            return
        if killed.returncode != 0:
            raise _docker_cleanup_error("Docker could not kill the exact managed container")
        inspected = _docker_cleanup_command(
            docker,
            ["container", "inspect", "--format", "{{.State.Running}}", container_id],
        )
        if _is_exact_container_not_found(inspected, container_id):
            return
        if inspected.returncode != 0:
            raise _docker_cleanup_error(
                "Docker could not inspect the exact managed container after kill"
            )
    removed = _docker_cleanup_command(docker, ["container", "rm", "--force", container_id])
    if removed.returncode != 0 and not _is_exact_container_not_found(removed, container_id):
        raise _docker_cleanup_error("Docker could not remove the exact managed container")
    verified = _docker_cleanup_command(
        docker, ["container", "inspect", "--format", "{{.Id}}", container_id]
    )
    if _is_exact_container_not_found(verified, container_id):
        return
    if verified.returncode != 0:
        raise _docker_cleanup_error("Docker could not verify the exact managed container cleanup")
    raise _docker_cleanup_error("Docker managed container still exists after cleanup")


def _run_managed_docker_container(
    docker: str,
    run_arguments: list[str],
    *,
    code: str,
    timeout_seconds: float = _DEFAULT_PROCESS_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = _MAXIMUM_WORKER_OUTPUT_BYTES,
    stderr_limit_bytes: int = _MAXIMUM_WORKER_ERROR_BYTES,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="vep-docker-cid-") as temporary:
        cid_path = Path(temporary) / "container.cid"
        command = [docker, "run", "--cidfile", str(cid_path), *run_arguments]
        try:
            completed = _run_bounded_process(
                command,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
        except _BoundedProcessFailure as exc:
            if exc.kind != "start_failure":
                _terminate_managed_docker_container(docker, cid_path, identity_required=True)
            raise _process_failure_error(code, command, exc) from None
        except BaseException:
            _terminate_managed_docker_container(docker, cid_path, identity_required=True)
            raise
        if completed.returncode == 0:
            _read_managed_container_id(cid_path, required=True)
            return completed
        _terminate_managed_docker_container(docker, cid_path, identity_required=False)
        if check:
            failure = subprocess.CalledProcessError(
                completed.returncode,
                command,
                output=completed.stdout,
                stderr=completed.stderr,
            )
            raise _process_failure_error(code, command, failure) from None
        return completed


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
    from video_evidence_pipeline.runtime_context import require_source_asr

    require_source_asr()
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
            "--build-arg",
            f"VEP_ASR_CONTRACT_SHA256={contract_identity}",
            str(repository_root),
        ],
        code="ASR_CONTAINER_BUILD_FAILED",
    )
    _require_image_identity(docker, profile.container_image, repository_root)
    return profile.container_image


def _gpu_preflight_failure_reason(
    docker: str, image: str, device_policy: str
) -> DeviceSelectionReason | None:
    try:
        completed = _run_managed_docker_container(
            docker,
            [
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
                    "import ctranslate2, json; "
                    "count = ctranslate2.get_cuda_device_count(); "
                    "print(json.dumps({'count': count, 'float16_indices': "
                    "[i for i in range(count) if 'float16' in "
                    "ctranslate2.get_supported_compute_types('cuda', i)]}))"
                ),
            ],
            code="ASR_CONTAINER_GPU_PREFLIGHT_FAILED",
            timeout_seconds=120,
            check=False,
        )
        if completed.returncode != 0:
            if completed.returncode in {125, 126, 127}:
                return DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE
            return DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE
        probe = json.loads(completed.stdout)
        if (
            not isinstance(probe, dict)
            or not isinstance(probe.get("count"), int)
            or not isinstance(probe.get("float16_indices"), list)
            or any(not isinstance(index, int) for index in probe["float16_indices"])
        ):
            return DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE
        if probe["count"] <= 0:
            return DeviceSelectionReason.NO_GPU_VISIBLE
        requested_index = (
            int(device_policy.split(":", 1)[1]) if device_policy.startswith("cuda:") else None
        )
        if requested_index is not None and requested_index >= probe["count"]:
            return DeviceSelectionReason.NO_GPU_VISIBLE
        if requested_index is not None and requested_index not in probe["float16_indices"]:
            return DeviceSelectionReason.GPU_COMPUTE_TYPE_UNSUPPORTED
        if requested_index is None and not probe["float16_indices"]:
            return DeviceSelectionReason.GPU_COMPUTE_TYPE_UNSUPPORTED
        return None
    except VepError as exc:
        if exc.code == "ASR_CONTAINER_CLEANUP_FAILED":
            raise
        return DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE
    except (json.JSONDecodeError, TypeError, ValueError):
        return DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE


def _validate_worker_response(
    request: AsrWorkerRequest,
    response: AsrWorkerResponse,
) -> AsrWorkerResponse:
    backend = response.backend
    if (
        backend.name != request.engine
        or backend.version != request.engine_version
        or backend.runtime != request.runtime
        or backend.runtime_version != request.runtime_version
        or backend.model_repository != request.model_repository
        or backend.model != request.model_repository
        or backend.model_revision != request.model_revision
        or backend.model_fingerprint is None
        or backend.model_fingerprint != request.model_storage_identity
        or backend.model_storage_identity != request.model_storage_identity
        or backend.device_policy != request.requested_device_policy
        or backend.container_image != request.container_image
        or backend.container_image_digest != request.container_image_digest
        or backend.configured_min_free_vram_mib != request.configured_min_free_vram_mib
    ):
        raise integrity_error(
            "ASR_WORKER_RESPONSE_INVALID",
            "ASR worker provenance does not match the host request",
        )
    selected_gpu_values = (
        backend.gpu_index,
        backend.gpu_name,
        backend.total_vram_mib,
        backend.free_vram_at_selection_mib,
    )
    candidate_gpu_values = (
        backend.candidate_gpu_index,
        backend.candidate_gpu_name,
        backend.candidate_total_vram_mib,
        backend.candidate_free_vram_mib,
    )
    capacity_contract_valid = True
    if all(value is not None for value in selected_gpu_values):
        capacity_contract_valid = backend.free_vram_at_selection_mib <= backend.total_vram_mib
    if all(value is not None for value in candidate_gpu_values):
        capacity_contract_valid = capacity_contract_valid and (
            backend.candidate_free_vram_mib <= backend.candidate_total_vram_mib
        )
    if backend.selected_device == "cuda":
        device_contract_valid = (
            backend.compute_type == request.gpu_compute_type
            and all(value is not None for value in selected_gpu_values)
            and backend.fallback is False
        )
    else:
        device_contract_valid = (
            backend.selected_device == "cpu"
            and backend.compute_type == request.cpu_compute_type
            and all(value is None for value in selected_gpu_values)
        )
    policy = request.requested_device_policy
    if policy == "cpu":
        policy_contract_valid = (
            backend.selected_device == "cpu"
            and backend.selection_reason is DeviceSelectionReason.EXPLICIT_CPU
            and backend.fallback is False
            and all(value is None for value in candidate_gpu_values)
        )
    elif policy.startswith("cuda"):
        requested_index = int(policy.split(":", 1)[1]) if ":" in policy else None
        policy_contract_valid = (
            backend.selected_device == "cuda"
            and backend.selection_reason is DeviceSelectionReason.EXPLICIT_CUDA_SELECTED
            and backend.fallback is False
            and (requested_index is None or backend.gpu_index == requested_index)
            and candidate_gpu_values == selected_gpu_values
        )
    elif backend.selected_device == "cuda":
        policy_contract_valid = (
            backend.selection_reason is DeviceSelectionReason.AUTO_GPU_SELECTED
            and backend.fallback is False
            and candidate_gpu_values == selected_gpu_values
            and backend.free_vram_at_selection_mib is not None
            and backend.free_vram_at_selection_mib >= request.configured_min_free_vram_mib
        )
    else:
        nonfallback_reasons = {
            DeviceSelectionReason.NO_GPU_VISIBLE,
            DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE,
            DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE,
            DeviceSelectionReason.GPU_COMPUTE_TYPE_UNSUPPORTED,
            DeviceSelectionReason.INSUFFICIENT_FREE_VRAM,
        }
        policy_contract_valid = (
            backend.fallback is True
            and backend.selection_reason is DeviceSelectionReason.GPU_RESOURCE_FALLBACK
        ) or (backend.fallback is False and backend.selection_reason in nonfallback_reasons)
        if request.gpu_unavailable_reason is not None:
            policy_contract_valid = policy_contract_valid and (
                backend.selection_reason is request.gpu_unavailable_reason
            )
        elif backend.selection_reason not in {
            DeviceSelectionReason.INSUFFICIENT_FREE_VRAM,
            DeviceSelectionReason.GPU_RESOURCE_FALLBACK,
        }:
            policy_contract_valid = False
        candidate_required = backend.selection_reason in {
            DeviceSelectionReason.INSUFFICIENT_FREE_VRAM,
            DeviceSelectionReason.GPU_RESOURCE_FALLBACK,
        }
        policy_contract_valid = policy_contract_valid and (
            all(value is not None for value in candidate_gpu_values)
            if candidate_required
            else all(value is None for value in candidate_gpu_values)
        )
        if backend.selection_reason is DeviceSelectionReason.INSUFFICIENT_FREE_VRAM:
            policy_contract_valid = policy_contract_valid and (
                backend.candidate_free_vram_mib is not None
                and backend.candidate_free_vram_mib < request.configured_min_free_vram_mib
            )
        elif backend.selection_reason is DeviceSelectionReason.GPU_RESOURCE_FALLBACK:
            policy_contract_valid = policy_contract_valid and (
                backend.candidate_free_vram_mib is not None
                and backend.candidate_free_vram_mib >= request.configured_min_free_vram_mib
            )
    fallback_contract_valid = (
        (
            backend.fallback is True
            and backend.selection_reason is DeviceSelectionReason.GPU_RESOURCE_FALLBACK
            and backend.fallback_from == "cuda"
            and backend.fallback_reason is not None
            and is_gpu_resource_error(backend.fallback_reason)
        )
        if backend.fallback is True
        else (
            backend.fallback is False
            and backend.fallback_from is None
            and backend.fallback_reason is None
        )
    )
    if (
        not capacity_contract_valid
        or not device_contract_valid
        or not policy_contract_valid
        or not fallback_contract_valid
    ):
        raise integrity_error(
            "ASR_WORKER_RESPONSE_INVALID",
            "ASR worker device provenance contradicts the host request",
        )
    if (
        len(response.segments) > request.configuration.maximum_segments
        or sum(len(item.raw_text) for item in response.segments)
        > request.configuration.maximum_total_text_chars
    ):
        raise integrity_error("ASR_WORKER_RESPONSE_INVALID", "ASR worker output exceeds limits")
    if any(
        not any(
            segment.start_ms >= interval.start_ms and segment.end_ms <= interval.end_ms
            for interval in request.intervals
        )
        for segment in response.segments
    ):
        raise integrity_error(
            "ASR_WORKER_RESPONSE_INVALID",
            "ASR worker returned a segment outside the requested intervals",
        )
    return response


def run_asr_worker(
    repository_root: Path,
    audio_path: Path,
    intervals: tuple[TranscriptInterval, ...],
    config: TranscriptionConfig,
) -> AsrWorkerResponse:
    from video_evidence_pipeline.runtime_context import require_source_asr

    require_source_asr()
    selected, profile = resolve_asr_profile(repository_root, config.asr_profile)
    docker = _docker()
    model, provision_manifest = require_provisioned_model(repository_root, selected, profile)
    from video_evidence_pipeline.transcription.provisioning import model_payload_identity

    identity, file_count, total_size = model_payload_identity(model, profile)
    if (
        identity != provision_manifest.model_storage_identity
        or file_count != provision_manifest.file_count
        or total_size != provision_manifest.total_size_bytes
    ):
        raise integrity_error(
            "ASR_MODEL_INVALID", "provisioned ASR model does not match its manifest"
        )
    digest = _require_image_identity(docker, profile.container_image, repository_root)
    gpu_unavailable_reason: DeviceSelectionReason | None = None
    if config.asr_device == "cpu":
        gpu_usable = False
    else:
        gpu_unavailable_reason = _gpu_preflight_failure_reason(docker, digest, config.asr_device)
        gpu_usable = gpu_unavailable_reason is None
    if config.asr_device.startswith("cuda") and not gpu_usable:
        raise configuration_error(
            "ASR_DEVICE_UNAVAILABLE", "explicit CUDA is unavailable to the ASR container"
        )
    request = AsrWorkerRequest(
        intervals=list(intervals),
        language_hint=config.asr_language,
        profile_id=selected,
        engine=profile.engine,
        engine_version=profile.engine_version,
        runtime=profile.runtime,
        runtime_version=profile.runtime_version,
        model=profile.model,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=provision_manifest.model_storage_identity,
        model_file_count=provision_manifest.file_count,
        model_total_size_bytes=provision_manifest.total_size_bytes,
        model_provisioned_at=provision_manifest.provisioned_at,
        requested_device_policy=config.asr_device,
        container_gpu_passthrough=gpu_usable,
        gpu_unavailable_reason=gpu_unavailable_reason,
        gpu_compute_type=profile.gpu_compute_type,
        cpu_compute_type=profile.cpu_compute_type,
        configured_min_free_vram_mib=profile.min_free_vram_mib,
        container_image=profile.container_image,
        container_image_digest=digest,
        configuration=config,
    )
    if not audio_path.is_file() or audio_path.is_symlink():
        raise integrity_error("ASR_WORKER_INPUT_INVALID", "ASR worker audio input is invalid")
    with tempfile.TemporaryDirectory(prefix="vep-asr-worker-") as temporary:
        request_path = Path(temporary) / "request.json"
        request_payload = pretty_json_bytes(request)
        if len(request_payload) > MAXIMUM_ASR_WORKER_REQUEST_BYTES:
            raise integrity_error(
                "ASR_WORKER_REQUEST_INVALID", "ASR worker request exceeds its byte limit"
            )
        request_path.write_bytes(request_payload)
        run_arguments = ["--rm", "--pull", "never", "--network", "none"]
        if gpu_usable:
            run_arguments.extend(["--gpus", "all"])
        run_arguments.extend(
            [
                "--mount",
                f"type=bind,src={audio_path},dst=/input/audio,readonly",
                "--mount",
                f"type=bind,src={model},dst=/model,readonly",
                "--mount",
                f"type=bind,src={request_path},dst=/request/request.json,readonly",
                digest,
                "asr",
                "worker",
                "--request",
                "/request/request.json",
                "--json",
            ]
        )
        raw = _run_managed_docker_container(
            docker,
            run_arguments,
            code="ASR_CONTAINER_TRANSCRIPTION_FAILED",
        ).stdout
    try:
        response = AsrWorkerResponse.model_validate_json(raw)
    except ValidationError:
        raise integrity_error(
            "ASR_WORKER_RESPONSE_INVALID", "ASR worker response is not valid typed JSON"
        ) from None
    return _validate_worker_response(request, response)


class ContainerAsrBackend:
    """Lazy host adapter; Docker is consulted only when caption gaps require ASR."""

    def __init__(self, repository_root: Path, config: TranscriptionConfig) -> None:
        self._repository_root = repository_root
        self._config = config
        self._final_identity: TranscriptionBackendIdentity | None = None

    def identity(self) -> TranscriptionBackendIdentity:
        if self._final_identity is not None:
            return self._final_identity
        raise configuration_error(
            "ASR_IDENTITY_NOT_FINAL",
            "ASR backend identity is available only after container inference",
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        intervals: tuple[TranscriptInterval, ...],
        configuration: TranscriptionConfig,
    ) -> list[AsrSegment]:
        if language != configuration.asr_language or configuration != self._config:
            raise integrity_error("ASR_WORKER_INPUT_INVALID", "ASR worker configuration changed")
        response = run_asr_worker(self._repository_root, audio_path, intervals, configuration)
        self._final_identity = response.backend
        return list(response.segments)


def run_transcription_container(workspace_path: Path, config: TranscriptionConfig) -> str:
    """Compatibility shim retained for callers; it no longer runs the pipeline in Docker."""

    from video_evidence_pipeline.application import transcribe_job
    from video_evidence_pipeline.storage.layout import JobWorkspace

    workspace = JobWorkspace.open(workspace_path)
    repository_root = workspace.resources_root
    result = transcribe_job(
        workspace.root,
        config,
        backend_override=ContainerAsrBackend(repository_root, config),
    )
    manifest = result.transcript_manifest
    return json.dumps(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": "TRANSCRIBED",
            "transcription_fingerprint": manifest.transcription_fingerprint,
            "segment_count": manifest.segment_count,
            "gap_count": len(manifest.gaps),
            "overlap_count": len(manifest.overlaps),
            "sources": [item.source.value for item in manifest.backends],
            "reused": result.reused,
        },
        ensure_ascii=False,
    )


def run_provision_container(repository_root: Path, profile_id: str | None = None) -> str:
    from video_evidence_pipeline.runtime_context import require_source_asr

    require_source_asr()
    selected, profile = resolve_asr_profile(repository_root, profile_id)
    from video_evidence_pipeline.transcription.provisioning import publish_model_payload

    with provision_lock(repository_root, selected, profile.model_revision):
        cleanup_stale_provision_staging(repository_root, selected)
        existing = existing_provisioned_model(repository_root, selected, profile)
        if existing is not None:
            model, manifest, reused = existing
        else:
            docker = _docker()
            digest = _require_image_identity(docker, profile.container_image, repository_root)
            with tempfile.TemporaryDirectory(prefix="vep-asr-provision-") as temporary:
                staging = Path(temporary)
                command = [
                    "--rm",
                    "--pull",
                    "never",
                    "--mount",
                    f"type=bind,src={staging},dst=/output",
                    digest,
                    "asr",
                    "provision-worker",
                    "--profile",
                    selected,
                    "--output",
                    "/output/model",
                    "--json",
                ]
                _run_managed_docker_container(
                    docker,
                    command,
                    code="ASR_MODEL_PROVISION_FAILED",
                )
                model, manifest, reused = publish_model_payload(
                    repository_root,
                    selected,
                    staging / "model",
                    expected_profile=profile,
                )
    return json.dumps(
        {
            "ok": True,
            "profile": manifest.profile,
            "model_repository": manifest.model_repository,
            "model_revision": manifest.model_revision,
            "model_storage_identity": manifest.model_storage_identity,
            "path": str(model),
            "reused": reused,
        }
    )


def smoke_asr_image(repository_root: Path, profile_id: str | None = None) -> dict[str, Any]:
    from video_evidence_pipeline.runtime_context import require_source_asr

    require_source_asr()
    docker = _docker()
    _selected, profile = resolve_asr_profile(repository_root, profile_id)
    digest = _require_image_identity(docker, profile.container_image, repository_root)
    code = (
        "import ctypes, importlib.metadata as m; "
        "import faster_whisper, ctranslate2; "
        f"assert m.version('faster-whisper') == '{profile.engine_version}'; "
        f"assert m.version('ctranslate2') == '{profile.runtime_version}'; "
        "ctypes.CDLL('libcublas.so.12'); ctypes.CDLL('libcudnn.so.9'); "
        "print('ok')"
    )
    _run_managed_docker_container(
        docker,
        [
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--entrypoint",
            "python3",
            digest,
            "-c",
            code,
        ],
        code="ASR_CONTAINER_SMOKE_FAILED",
    )
    _run_managed_docker_container(
        docker,
        [
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            digest,
            "asr",
            "worker",
            "--help",
        ],
        code="ASR_CONTAINER_SMOKE_FAILED",
    )
    lifecycle_timeout_code = "ASR_CONTAINER_LIFECYCLE_SMOKE_TIMEOUT"
    try:
        _run_managed_docker_container(
            docker,
            [
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--entrypoint",
                "python3",
                digest,
                "-c",
                "import time; time.sleep(30)",
            ],
            code=lifecycle_timeout_code,
            timeout_seconds=2,
        )
    except VepError as exc:
        if (
            exc.code != lifecycle_timeout_code
            or exc.safe_message != "ASR container command timed out"
        ):
            raise
    else:  # pragma: no cover - actual image command must outlive the injected timeout
        raise integrity_error(
            "ASR_CONTAINER_LIFECYCLE_SMOKE_FAILED",
            "ASR lifecycle smoke container unexpectedly completed before timeout",
        )
    return {
        "image": profile.container_image,
        "identity": digest,
        "contract": "valid",
        "managed_lifecycle": "verified",
    }


def preflight_asr_gpu(
    repository_root: Path,
    profile_id: str | None = None,
    device_policy: str = "auto",
) -> dict[str, Any]:
    from video_evidence_pipeline.runtime_context import require_source_asr

    require_source_asr()
    if re.fullmatch(r"(?:auto|cuda|cuda:[0-9]+)", device_policy) is None:
        raise configuration_error(
            "ASR_DEVICE_INVALID", "GPU preflight requires auto, cuda, or cuda:<index>"
        )
    docker = _docker()
    _selected, profile = resolve_asr_profile(repository_root, profile_id)
    digest = _require_image_identity(docker, profile.container_image, repository_root)
    reason = _gpu_preflight_failure_reason(docker, digest, device_policy)
    return {
        "image": profile.container_image,
        "identity": digest,
        "device_policy": device_policy,
        "gpu_usable": reason is None,
        "reason": reason.value if reason is not None else None,
    }
