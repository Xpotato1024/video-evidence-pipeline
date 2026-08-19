"""Deterministic ASR device selection independent of the inference backend."""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass

from video_evidence_pipeline.errors import configuration_error
from video_evidence_pipeline.transcription.profiles import AsrProfile


@dataclass(frozen=True, slots=True)
class GpuDevice:
    index: int
    name: str
    total_vram_mib: int
    free_vram_mib: int


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    gpus: tuple[GpuDevice, ...]
    container_gpu_passthrough: bool
    ctranslate2_cuda_available: bool
    cuda_compute_types: frozenset[str]


@dataclass(frozen=True, slots=True)
class DeviceSelection:
    device_policy: str
    selected_device: str
    compute_type: str
    gpu: GpuDevice | None


def _explicit_index(device_policy: str) -> int | None:
    if device_policy.startswith("cuda:"):
        return int(device_policy.split(":", 1)[1])
    return None


def select_device(
    profile: AsrProfile,
    device_policy: str,
    capabilities: RuntimeCapabilities,
) -> DeviceSelection:
    if device_policy not in {"auto", "cpu", "cuda"} and not device_policy.startswith("cuda:"):
        raise configuration_error("ASR_DEVICE_INVALID", "ASR device policy is invalid")
    if device_policy == "cpu":
        return DeviceSelection(device_policy, "cpu", profile.cpu_compute_type, None)

    cuda_runtime_usable = (
        capabilities.container_gpu_passthrough
        and capabilities.ctranslate2_cuda_available
        and profile.gpu_compute_type in capabilities.cuda_compute_types
    )
    candidates = list(capabilities.gpus) if cuda_runtime_usable else []
    requested_index = _explicit_index(device_policy)
    if requested_index is not None:
        candidates = [gpu for gpu in candidates if gpu.index == requested_index]
    if device_policy == "auto":
        candidates = [gpu for gpu in candidates if gpu.free_vram_mib >= profile.min_free_vram_mib]
        if not candidates:
            return DeviceSelection(device_policy, "cpu", profile.cpu_compute_type, None)
    elif not candidates:
        raise configuration_error(
            "ASR_DEVICE_UNAVAILABLE",
            "explicit CUDA device is not usable by the containerized CTranslate2 runtime",
        )

    selected = max(candidates, key=lambda gpu: (gpu.free_vram_mib, -gpu.index))
    return DeviceSelection(device_policy, "cuda", profile.gpu_compute_type, selected)


def is_gpu_resource_error(exc: BaseException) -> bool:
    """Recognize only resource exhaustion; invalid input/configuration must surface."""

    message = str(exc).casefold()
    markers = (
        "cuda out of memory",
        "cuda_error_out_of_memory",
        "cublas_status_alloc_failed",
        "cuda resource exhaustion",
        "cuda allocation failure",
        "failed to allocate cuda",
    )
    return any(marker in message for marker in markers)


def probe_nvidia_gpus() -> tuple[GpuDevice, ...]:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ()
    devices: list[GpuDevice] = []
    try:
        for row in csv.reader(io.StringIO(completed.stdout)):
            if len(row) != 4:
                return ()
            devices.append(
                GpuDevice(
                    index=int(row[0].strip()),
                    name=row[1].strip(),
                    total_vram_mib=int(row[2].strip()),
                    free_vram_mib=int(row[3].strip()),
                )
            )
    except ValueError:
        return ()
    return tuple(sorted(devices, key=lambda gpu: gpu.index))
