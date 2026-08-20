"""Deterministic ASR device selection independent of the inference backend."""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass

from video_evidence_pipeline.domain.transcription import DeviceSelectionReason
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
    cuda_compute_types_by_gpu: tuple[tuple[int, frozenset[str]], ...] = ()
    unavailable_reason: DeviceSelectionReason | None = None


@dataclass(frozen=True, slots=True)
class DeviceSelection:
    device_policy: str
    selected_device: str
    compute_type: str
    gpu: GpuDevice | None
    reason: DeviceSelectionReason
    candidate_gpu: GpuDevice | None = None


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
        return DeviceSelection(
            device_policy,
            "cpu",
            profile.cpu_compute_type,
            None,
            DeviceSelectionReason.EXPLICIT_CPU,
        )

    ordered = sorted(capabilities.gpus, key=lambda gpu: (-gpu.free_vram_mib, gpu.index))
    candidate = ordered[0] if ordered else None
    per_gpu = dict(capabilities.cuda_compute_types_by_gpu)
    compute_eligible = [
        gpu
        for gpu in ordered
        if profile.gpu_compute_type in per_gpu.get(gpu.index, capabilities.cuda_compute_types)
    ]
    cuda_runtime_usable = (
        capabilities.container_gpu_passthrough
        and capabilities.ctranslate2_cuda_available
        and profile.gpu_compute_type in capabilities.cuda_compute_types
    )
    candidates = list(compute_eligible) if cuda_runtime_usable else []
    requested_index = _explicit_index(device_policy)
    if requested_index is not None:
        candidates = [gpu for gpu in candidates if gpu.index == requested_index]
    if device_policy == "auto":
        candidates = [gpu for gpu in candidates if gpu.free_vram_mib >= profile.min_free_vram_mib]
        if not candidates:
            if capabilities.unavailable_reason is not None:
                reason = capabilities.unavailable_reason
            elif not capabilities.container_gpu_passthrough:
                reason = DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE
            elif not ordered:
                reason = DeviceSelectionReason.NO_GPU_VISIBLE
            elif not capabilities.ctranslate2_cuda_available:
                reason = DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE
            elif not compute_eligible:
                reason = DeviceSelectionReason.GPU_COMPUTE_TYPE_UNSUPPORTED
            else:
                reason = DeviceSelectionReason.INSUFFICIENT_FREE_VRAM
            return DeviceSelection(
                device_policy,
                "cpu",
                profile.cpu_compute_type,
                None,
                reason,
                candidate,
            )
    elif not candidates:
        raise configuration_error(
            "ASR_DEVICE_UNAVAILABLE",
            "explicit CUDA device is not usable by the containerized CTranslate2 runtime",
        )

    selected = min(candidates, key=lambda gpu: (-gpu.free_vram_mib, gpu.index))
    reason = (
        DeviceSelectionReason.AUTO_GPU_SELECTED
        if device_policy == "auto"
        else DeviceSelectionReason.EXPLICIT_CUDA_SELECTED
    )
    return DeviceSelection(
        device_policy,
        "cuda",
        profile.gpu_compute_type,
        selected,
        reason,
        selected,
    )


def is_gpu_resource_error(error: BaseException | str) -> bool:
    """Recognize only resource exhaustion; invalid input/configuration must surface."""

    message = " ".join(str(error).casefold().replace("_", " ").split())
    markers = (
        "cuda failed with error out of memory",
        "cuda out of memory",
        "cuda error out of memory",
        "cublas failed with status cublas status alloc failed",
        "cublas status alloc failed",
        "cudnn failed with status cudnn status alloc failed",
        "cudnn status alloc failed",
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
