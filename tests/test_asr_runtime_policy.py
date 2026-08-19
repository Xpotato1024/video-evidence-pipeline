from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import video_evidence_pipeline.cli as cli_module
import video_evidence_pipeline.transcription.provisioning as provisioning_module
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.domain.transcription import (
    AsrWorkerRequest,
    AsrWorkerResponse,
    DeviceSelectionReason,
    TranscriptInterval,
    TranscriptionBackendIdentity,
    TranscriptionConfig,
    TranscriptManifest,
    TranscriptSource,
)
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.transcription import container as container_module
from video_evidence_pipeline.transcription import device as device_module
from video_evidence_pipeline.transcription.device import (
    GpuDevice,
    RuntimeCapabilities,
    is_gpu_resource_error,
    select_device,
)
from video_evidence_pipeline.transcription.profiles import (
    DEFAULT_ASR_PROFILE,
    AsrModelProvisionManifest,
    model_manifest_path,
    model_storage_directory,
    require_provisioned_model,
    resolve_asr_profile,
)
from video_evidence_pipeline.transcription.provisioning import (
    cleanup_stale_provision_staging,
    model_payload_identity,
    provision_lock,
    provision_model,
)


def _profile():
    selected, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)
    assert selected == DEFAULT_ASR_PROFILE
    return profile


def _write_profile_model(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / "model.bin").write_bytes(b"model")
    (directory / "preprocessor_config.json").write_text('{"feature_size": 128}', encoding="utf-8")
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")


def _caps(
    *gpus: GpuDevice,
    passthrough: bool = True,
    cuda: bool = True,
    compute: frozenset[str] = frozenset({"float16"}),
) -> RuntimeCapabilities:
    return RuntimeCapabilities(tuple(gpus), passthrough, cuda, compute)


def _worker_request(device: str = "cpu") -> AsrWorkerRequest:
    profile = _profile()
    return AsrWorkerRequest(
        intervals=[TranscriptInterval(start_ms=0, end_ms=1000)],
        profile_id=DEFAULT_ASR_PROFILE,
        engine=profile.engine,
        engine_version=profile.engine_version,
        runtime=profile.runtime,
        runtime_version=profile.runtime_version,
        model=profile.model,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity="1" * 64,
        model_file_count=4,
        model_total_size_bytes=64,
        model_provisioned_at=datetime.now(UTC),
        requested_device_policy=device,
        container_gpu_passthrough=device != "cpu",
        gpu_compute_type=profile.gpu_compute_type,
        cpu_compute_type=profile.cpu_compute_type,
        configured_min_free_vram_mib=profile.min_free_vram_mib,
        container_image=profile.container_image,
        container_image_digest="sha256:" + "2" * 64,
        configuration=TranscriptionConfig(asr_profile=DEFAULT_ASR_PROFILE, asr_device=device),
    )


def test_default_profile_is_pinned_and_repository_owned() -> None:
    profile = _profile()
    assert profile.engine == "faster-whisper"
    assert profile.runtime == "CTranslate2"
    assert profile.model == "large-v3"
    assert profile.model_repository == "Systran/faster-whisper-large-v3"
    assert profile.model_revision == "edaa852ec7e145841d8ffdb056a99866b5f0a478"
    assert profile.gpu_compute_type == "float16"
    assert profile.cpu_compute_type == "int8"
    assert profile.min_free_vram_mib == 6144


def test_profiled_runtime_conditions_are_exported_to_json_schema() -> None:
    config_schema = TranscriptionConfig.model_json_schema()
    backend_schema = TranscriptionBackendIdentity.model_json_schema()
    assert config_schema["allOf"][0]["then"]["properties"]["asr_compute_type"] == {"const": "auto"}
    required = set(backend_schema["allOf"][0]["then"]["required"])
    assert {
        "runtime_version",
        "model_repository",
        "model_revision",
        "model_storage_identity",
        "selection_reason",
        "selected_device",
        "compute_type",
        "container_image_digest",
        "fallback",
    } <= required
    assert backend_schema["allOf"][0]["then"]["properties"]["runtime_version"] == {
        "not": {"type": "null"}
    }
    transcript_condition = TranscriptManifest.model_json_schema()["allOf"][0]
    assert "asr_profile" in transcript_condition["if"]["properties"]["configuration"]["required"]
    assert (
        "model_revision"
        in transcript_condition["then"]["properties"]["backends"]["contains"]["required"]
    )
    transcript_backend = transcript_condition["then"]["properties"]["backends"]["contains"]
    assert transcript_backend["properties"]["runtime"] == {"not": {"type": "null"}}
    assert transcript_backend["allOf"][0]["then"]["properties"]["selection_reason"] == {
        "not": {"type": "null"}
    }


@pytest.mark.parametrize("case", ["runtime", "explicit_cpu_cuda", "explicit_cuda_cpu"])
def test_host_rejects_contradictory_worker_provenance(case: str) -> None:
    request = _worker_request("cuda" if case == "explicit_cuda_cpu" else "cpu")
    profile = _profile()
    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.ASR,
        name=profile.engine,
        version=profile.engine_version,
        runtime=profile.runtime,
        runtime_version=profile.runtime_version,
        model=profile.model_repository,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=request.model_storage_identity,
        device_policy=request.requested_device_policy,
        selection_reason=(
            DeviceSelectionReason.GPU_RESOURCE_FALLBACK
            if case == "explicit_cuda_cpu"
            else DeviceSelectionReason.EXPLICIT_CPU
        ),
        selected_device="cpu",
        configured_min_free_vram_mib=profile.min_free_vram_mib,
        compute_type=profile.cpu_compute_type,
        container_image=profile.container_image,
        container_image_digest=request.container_image_digest,
        fallback=case == "explicit_cuda_cpu",
        fallback_from="cuda" if case == "explicit_cuda_cpu" else None,
        fallback_reason="CUDA failed with error out of memory"
        if case == "explicit_cuda_cpu"
        else None,
    )
    if case == "runtime":
        backend = backend.model_copy(update={"runtime": "bogus-runtime"})
    elif case == "explicit_cpu_cuda":
        backend = backend.model_copy(
            update={
                "selection_reason": DeviceSelectionReason.AUTO_GPU_SELECTED,
                "selected_device": "cuda",
                "gpu_index": 0,
                "gpu_name": "synthetic",
                "total_vram_mib": 8192,
                "free_vram_at_selection_mib": 7000,
                "compute_type": profile.gpu_compute_type,
            }
        )
    with pytest.raises(VepError) as rejected:
        container_module._validate_worker_response(
            request, AsrWorkerResponse(backend=backend, segments=[])
        )
    assert rejected.value.code == "ASR_WORKER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("policy", "reason", "fallback", "with_candidate"),
    [
        ("auto", DeviceSelectionReason.NO_GPU_VISIBLE, False, False),
        ("cpu", DeviceSelectionReason.EXPLICIT_CPU, False, True),
        ("auto", DeviceSelectionReason.GPU_RESOURCE_FALLBACK, True, False),
    ],
)
def test_host_rejects_worker_candidate_gpu_provenance_gaps(
    policy: str,
    reason: DeviceSelectionReason,
    fallback: bool,
    with_candidate: bool,
) -> None:
    request = _worker_request(policy)
    profile = _profile()
    candidate = {
        "candidate_gpu_index": 0,
        "candidate_gpu_name": "synthetic",
        "candidate_total_vram_mib": 8192,
        "candidate_free_vram_mib": 7000,
    }
    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.ASR,
        name=profile.engine,
        version=profile.engine_version,
        runtime=profile.runtime,
        runtime_version=profile.runtime_version,
        model=profile.model_repository,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=request.model_storage_identity,
        device_policy=policy,
        selection_reason=reason,
        selected_device="cpu",
        configured_min_free_vram_mib=profile.min_free_vram_mib,
        compute_type=profile.cpu_compute_type,
        container_image=profile.container_image,
        container_image_digest=request.container_image_digest,
        fallback=fallback,
        fallback_from="cuda" if fallback else None,
        fallback_reason="CUDA failed with error out of memory" if fallback else None,
        **(candidate if with_candidate else {}),
    )
    with pytest.raises(VepError) as rejected:
        container_module._validate_worker_response(
            request, AsrWorkerResponse(backend=backend, segments=[])
        )
    assert rejected.value.code == "ASR_WORKER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("reason", "selected_device", "total", "free"),
    [
        (DeviceSelectionReason.INSUFFICIENT_FREE_VRAM, "cpu", 8192, 7000),
        (DeviceSelectionReason.AUTO_GPU_SELECTED, "cuda", 8192, 100),
        (DeviceSelectionReason.AUTO_GPU_SELECTED, "cuda", 100, 7000),
    ],
)
def test_host_rejects_impossible_gpu_capacity_and_threshold_provenance(
    reason: DeviceSelectionReason,
    selected_device: str,
    total: int,
    free: int,
) -> None:
    request = _worker_request("auto")
    profile = _profile()
    selected = (
        {
            "gpu_index": 0,
            "gpu_name": "synthetic",
            "total_vram_mib": total,
            "free_vram_at_selection_mib": free,
        }
        if selected_device == "cuda"
        else {}
    )
    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.ASR,
        name=profile.engine,
        version=profile.engine_version,
        runtime=profile.runtime,
        runtime_version=profile.runtime_version,
        model=profile.model_repository,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=request.model_storage_identity,
        device_policy="auto",
        selection_reason=reason,
        selected_device=selected_device,
        configured_min_free_vram_mib=profile.min_free_vram_mib,
        candidate_gpu_index=0,
        candidate_gpu_name="synthetic",
        candidate_total_vram_mib=total,
        candidate_free_vram_mib=free,
        compute_type=(
            profile.gpu_compute_type if selected_device == "cuda" else profile.cpu_compute_type
        ),
        container_image=profile.container_image,
        container_image_digest=request.container_image_digest,
        fallback=False,
        **selected,
    )
    with pytest.raises(VepError) as rejected:
        container_module._validate_worker_response(
            request, AsrWorkerResponse(backend=backend, segments=[])
        )
    assert rejected.value.code == "ASR_WORKER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"count": 0, "float16_indices": []}', DeviceSelectionReason.NO_GPU_VISIBLE),
        (
            '{"count": 1, "float16_indices": []}',
            DeviceSelectionReason.GPU_COMPUTE_TYPE_UNSUPPORTED,
        ),
        ('{"count": 1, "float16_indices": [0]}', None),
    ],
)
def test_container_gpu_preflight_preserves_backend_and_compute_failure_reasons(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: DeviceSelectionReason | None,
) -> None:
    monkeypatch.setattr(
        container_module.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(stdout=stdout),
    )
    assert container_module._gpu_preflight_failure_reason("docker", "image", "auto") is expected


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (1, DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE),
        (125, DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE),
    ],
)
def test_container_gpu_preflight_classifies_process_and_docker_failures(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected: DeviceSelectionReason,
) -> None:
    def fail(*_args, **_kwargs):
        raise container_module.subprocess.CalledProcessError(returncode, ["docker", "run"])

    monkeypatch.setattr(container_module.subprocess, "run", fail)
    assert container_module._gpu_preflight_failure_reason("docker", "image", "auto") is expected


def test_explicit_cuda_index_is_rejected_during_container_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        container_module.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            stdout='{"count": 1, "float16_indices": [0]}'
        ),
    )
    assert (
        container_module._gpu_preflight_failure_reason("docker", "image", "cuda:1")
        is DeviceSelectionReason.NO_GPU_VISIBLE
    )


def test_profiled_config_rejects_legacy_compute_type_override() -> None:
    with pytest.raises(ValidationError):
        TranscriptionConfig(asr_profile=DEFAULT_ASR_PROFILE, asr_compute_type="int8")


def test_container_build_contract_pins_base_and_python_dependencies() -> None:
    dockerfile = (Path.cwd() / "containers" / "asr" / "Dockerfile").read_text(encoding="utf-8")
    first_line = dockerfile.splitlines()[0]
    assert re.fullmatch(r"FROM nvidia/cuda:[^@\s]+@sha256:[0-9a-f]{64}", first_line)
    requirements = (
        (Path.cwd() / "containers" / "asr" / "requirements.lock")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert requirements
    assert all("==" in item and not item.startswith("#") for item in requirements)
    assert "hatchling==1.31.0" in requirements
    assert "--no-build-isolation --no-deps ." in dockerfile
    assert "12.6.3-cudnn-runtime-ubuntu24.04" in first_line
    assert re.fullmatch(r"[0-9a-f]{64}", container_module._container_contract_identity(Path.cwd()))


def test_container_execution_rejects_missing_or_stale_build_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_module, "_image_digest", lambda *_: None)
    monkeypatch.setattr(container_module, "_container_contract_identity", lambda *_: "a" * 64)
    with pytest.raises(VepError) as missing:
        container_module._require_image_identity("docker", "vep-asr:1.0.0", Path.cwd())
    assert missing.value.code == "ASR_CONTAINER_IDENTITY_UNAVAILABLE"

    monkeypatch.setattr(container_module, "_image_digest", lambda *_: "sha256:" + "b" * 64)
    monkeypatch.setattr(container_module, "_image_contract_label", lambda *_: "c" * 64)
    with pytest.raises(VepError) as stale:
        container_module._require_image_identity("docker", "vep-asr:1.0.0", Path.cwd())
    assert stale.value.code == "ASR_CONTAINER_IDENTITY_UNAVAILABLE"


def test_container_build_contract_is_bound_to_resolved_immutable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "sha256:" + "b" * 64
    inspected: list[str] = []
    monkeypatch.setattr(container_module, "_image_digest", lambda *_: digest)
    monkeypatch.setattr(container_module, "_container_contract_identity", lambda *_: "a" * 64)

    def contract_label(_docker: str, image: str) -> str:
        inspected.append(image)
        return "a" * 64

    monkeypatch.setattr(container_module, "_image_contract_label", contract_label)

    resolved = container_module._require_image_identity("docker", "vep-asr:1.0.0", Path.cwd())

    assert resolved == digest
    assert inspected == [digest]


@pytest.mark.parametrize(
    "capabilities",
    [
        _caps(),
        _caps(GpuDevice(0, "gpu", 8192, 7000), passthrough=False),
        _caps(GpuDevice(0, "gpu", 8192, 7000), cuda=False),
        _caps(GpuDevice(0, "gpu", 8192, 7000), compute=frozenset({"int8"})),
        _caps(GpuDevice(0, "gpu", 8192, 6143)),
    ],
)
def test_auto_selects_cpu_when_any_gpu_eligibility_gate_fails(
    capabilities: RuntimeCapabilities,
) -> None:
    selection = select_device(_profile(), "auto", capabilities)
    assert selection.selected_device == "cpu"
    assert selection.compute_type == "int8"


def test_device_selection_reasons_and_explicit_cpu_are_structured() -> None:
    profile = _profile()
    assert select_device(profile, "cpu", _caps()).reason is DeviceSelectionReason.EXPLICIT_CPU
    assert select_device(profile, "auto", _caps()).reason is DeviceSelectionReason.NO_GPU_VISIBLE
    assert (
        select_device(profile, "auto", _caps(passthrough=False)).reason
        is DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE
    )
    gpu = GpuDevice(0, "gpu", 8192, 7000)
    assert (
        select_device(profile, "auto", _caps(gpu, cuda=False)).reason
        is DeviceSelectionReason.BACKEND_CUDA_UNAVAILABLE
    )
    assert (
        select_device(profile, "auto", _caps(gpu, compute=frozenset({"int8"}))).reason
        is DeviceSelectionReason.GPU_COMPUTE_TYPE_UNSUPPORTED
    )
    insufficient = select_device(profile, "auto", _caps(GpuDevice(0, "gpu", 8192, 6143)))
    assert insufficient.reason is DeviceSelectionReason.INSUFFICIENT_FREE_VRAM
    assert insufficient.gpu is None
    assert insufficient.candidate_gpu is not None


@pytest.mark.parametrize("free", [6144, 7000])
def test_auto_selects_gpu_at_or_above_absolute_threshold(free: int) -> None:
    selection = select_device(_profile(), "auto", _caps(GpuDevice(0, "gpu", 8192, free)))
    assert selection.selected_device == "cuda"
    assert selection.compute_type == "float16"
    assert selection.gpu is not None and selection.gpu.index == 0


def test_multi_gpu_selection_and_explicit_semantics() -> None:
    capabilities = _caps(
        GpuDevice(0, "small", 8192, 6500),
        GpuDevice(1, "large", 16384, 12000),
    )
    assert select_device(_profile(), "auto", capabilities).gpu.index == 1  # type: ignore[union-attr]
    assert select_device(_profile(), "cuda:0", capabilities).gpu.index == 0  # type: ignore[union-attr]
    assert select_device(_profile(), "cpu", capabilities).selected_device == "cpu"
    with pytest.raises(VepError) as unavailable:
        select_device(_profile(), "cuda:2", capabilities)
    assert unavailable.value.code == "ASR_DEVICE_UNAVAILABLE"
    tied = _caps(GpuDevice(2, "later", 8192, 7000), GpuDevice(1, "earlier", 8192, 7000))
    assert select_device(_profile(), "auto", tied).gpu.index == 1  # type: ignore[union-attr]


def test_only_gpu_resource_failures_are_fallback_eligible() -> None:
    assert is_gpu_resource_error(RuntimeError("CUDA failed with error out of memory"))
    assert is_gpu_resource_error(
        RuntimeError("cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED")
    )
    assert is_gpu_resource_error(RuntimeError("cuDNN failed with status CUDNN_STATUS_ALLOC_FAILED"))
    assert not is_gpu_resource_error(RuntimeError("CUDA failed with error invalid device"))
    assert not is_gpu_resource_error(
        RuntimeError("cuDNN failed with status CUDNN_STATUS_BAD_PARAM")
    )
    assert not is_gpu_resource_error(ValueError("invalid input"))
    assert not is_gpu_resource_error(RuntimeError("corrupted model"))


def test_inference_requires_preprovisioned_pinned_model(tmp_path: Path) -> None:
    profile = _profile()
    with pytest.raises(VepError) as missing:
        require_provisioned_model(tmp_path, DEFAULT_ASR_PROFILE, profile)
    assert missing.value.code == "ASR_MODEL_NOT_PROVISIONED"


def test_pinned_provision_manifest_selects_persistent_repository_model(tmp_path: Path) -> None:
    profile = _profile()
    model = model_storage_directory(tmp_path, DEFAULT_ASR_PROFILE, profile)
    model.mkdir(parents=True)
    _write_profile_model(model)
    manifest = AsrModelProvisionManifest(
        profile=DEFAULT_ASR_PROFILE,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity="a" * 64,
        file_count=3,
        total_size_bytes=9,
        provisioned_at=datetime.now(UTC),
    )
    model_manifest_path(tmp_path, DEFAULT_ASR_PROFILE, profile).write_bytes(
        pretty_json_bytes(manifest)
    )

    selected_path, selected_manifest = require_provisioned_model(
        tmp_path, DEFAULT_ASR_PROFILE, profile
    )

    assert selected_path == model
    assert selected_manifest.model_revision == profile.model_revision
    assert str(selected_path).startswith(str(tmp_path / ".local" / "models"))


def test_explicit_provisioning_publishes_model_and_manifest_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    calls: list[tuple[str, str]] = []

    def snapshot_download(*, repo_id: str, revision: str, local_dir: Path, token: bool) -> None:
        assert token is False
        calls.append((repo_id, revision))
        _write_profile_model(local_dir)

    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    model, manifest, reused = provision_model(tmp_path, DEFAULT_ASR_PROFILE)
    second_model, second_manifest, second_reused = provision_model(tmp_path, DEFAULT_ASR_PROFILE)

    assert calls == [(manifest.model_repository, manifest.model_revision)]
    assert reused is False
    assert second_reused is True
    assert second_model == model
    assert second_manifest == manifest
    assert model.is_dir()
    assert model_manifest_path(tmp_path, DEFAULT_ASR_PROFILE, _profile()).is_file()
    assert not [path for path in model.parent.parent.glob(".provision-*") if path.is_dir()]
    (model / "model.bin").write_bytes(b"tampered")
    with pytest.raises(VepError) as mismatch:
        provision_model(tmp_path, DEFAULT_ASR_PROFILE)
    assert mismatch.value.code == "ASR_MODEL_INVALID"


def test_public_provision_serializes_download_and_reuses_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    worker_calls = 0
    docker_calls = 0
    simultaneous_start = threading.Barrier(2)

    def docker() -> str:
        nonlocal docker_calls
        docker_calls += 1
        return "docker"

    def run(command: list[str], *, code: str):
        nonlocal worker_calls
        assert code == "ASR_MODEL_PROVISION_FAILED"
        worker_calls += 1
        time.sleep(0.1)
        mount = next(item for item in command if "dst=/output" in item)
        source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        _write_profile_model(source / "model")
        return types.SimpleNamespace(stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(container_module, "_docker", docker)
    monkeypatch.setattr(
        container_module, "_require_image_identity", lambda *_: "sha256:" + "a" * 64
    )
    monkeypatch.setattr(container_module, "_run", run)

    def provision_after_barrier(_index: int) -> str:
        simultaneous_start.wait()
        return container_module.run_provision_container(tmp_path, DEFAULT_ASR_PROFILE)

    with ThreadPoolExecutor(max_workers=2) as executor:
        payloads = list(executor.map(provision_after_barrier, range(2)))

    assert worker_calls == 1
    assert docker_calls == 1
    assert sorted(json.loads(payload)["reused"] for payload in payloads) == [False, True]

    worker_calls = 0
    docker_calls = 0
    download_calls = 0

    def forbidden_download(*_args, **_kwargs) -> None:
        nonlocal download_calls
        download_calls += 1
        pytest.fail("valid public reuse must not download")

    def forbidden_docker() -> str:
        nonlocal docker_calls
        docker_calls += 1
        pytest.fail("valid reuse must not consult Docker")

    monkeypatch.setattr(container_module, "_docker", forbidden_docker)
    monkeypatch.setattr(provisioning_module, "download_model_payload", forbidden_download)
    reused = json.loads(container_module.run_provision_container(tmp_path, DEFAULT_ASR_PROFILE))
    assert reused["reused"] is True
    assert docker_calls == 0
    assert worker_calls == 0
    assert download_calls == 0

    profile = _profile()
    model = model_storage_directory(tmp_path, DEFAULT_ASR_PROFILE, profile)
    (model / "model.bin").write_bytes(b"tampered")
    with pytest.raises(VepError) as inconsistent:
        container_module.run_provision_container(tmp_path, DEFAULT_ASR_PROFILE)
    assert inconsistent.value.code == "ASR_MODEL_INVALID"
    assert worker_calls == 0


def test_public_provision_cleans_only_safe_stale_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    profile_root = tmp_path / ".local" / "models" / "asr" / DEFAULT_ASR_PROFILE
    stale = profile_root / (".provision-" + "a" * 32)
    unknown = profile_root / ".provision-unknown"
    stale.mkdir(parents=True)
    unknown.mkdir()
    (stale / "partial.bin").write_bytes(b"partial")
    (unknown / "owned.bin").write_bytes(b"owned")

    def run(command: list[str], *, code: str):
        mount = next(item for item in command if "dst=/output" in item)
        source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        _write_profile_model(source / "model")
        return types.SimpleNamespace(stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(container_module, "_docker", lambda: "docker")
    monkeypatch.setattr(
        container_module, "_require_image_identity", lambda *_: "sha256:" + "a" * 64
    )
    monkeypatch.setattr(container_module, "_run", run)
    container_module.run_provision_container(tmp_path, DEFAULT_ASR_PROFILE)

    assert not stale.exists()
    assert unknown.is_dir()


def test_public_provision_rejects_profile_revision_drift_during_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    original_profile = _profile()

    def run(command: list[str], *, code: str):
        assert code == "ASR_MODEL_PROVISION_FAILED"
        mount = next(item for item in command if "dst=/output" in item)
        source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        _write_profile_model(source / "model")
        registry = tmp_path / "config" / "asr-profiles.yaml"
        registry.write_text(
            registry.read_text(encoding="utf-8").replace(
                original_profile.model_revision,
                "f" * 40,
            ),
            encoding="utf-8",
        )
        return types.SimpleNamespace(stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(container_module, "_docker", lambda: "docker")
    monkeypatch.setattr(
        container_module, "_require_image_identity", lambda *_: "sha256:" + "a" * 64
    )
    monkeypatch.setattr(container_module, "_run", run)

    with pytest.raises(VepError) as changed:
        container_module.run_provision_container(tmp_path, DEFAULT_ASR_PROFILE)

    assert changed.value.code == "ASR_PROFILE_CHANGED"
    assert not model_storage_directory(tmp_path, DEFAULT_ASR_PROFILE, original_profile).exists()


def test_profile_storage_lock_prevents_cross_revision_staging_cleanup(tmp_path: Path) -> None:
    profile_root = tmp_path / ".local" / "models" / "asr" / DEFAULT_ASR_PROFILE
    staging = profile_root / (".provision-" + "b" * 32)
    first_entered = threading.Event()
    second_attempting = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    failures: list[BaseException] = []

    def first_revision() -> None:
        try:
            with provision_lock(tmp_path, DEFAULT_ASR_PROFILE, "1" * 40):
                staging.mkdir()
                first_entered.set()
                assert release_first.wait(timeout=5)
        except BaseException as exc:
            failures.append(exc)

    def second_revision() -> None:
        try:
            assert first_entered.wait(timeout=5)
            second_attempting.set()
            with provision_lock(tmp_path, DEFAULT_ASR_PROFILE, "2" * 40):
                second_entered.set()
                cleanup_stale_provision_staging(tmp_path, DEFAULT_ASR_PROFILE)
        except BaseException as exc:
            failures.append(exc)

    first_thread = threading.Thread(target=first_revision)
    second_thread = threading.Thread(target=second_revision)
    first_thread.start()
    second_thread.start()
    assert second_attempting.wait(timeout=5)
    assert not second_entered.wait(timeout=0.2)
    assert staging.is_dir()
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not failures
    assert second_entered.is_set()
    assert not staging.exists()


def test_internal_worker_commands_reject_normal_host_and_hide_arbitrary_model_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VEP_ASR_INTERNAL_RUNTIME", raising=False)
    monkeypatch.delenv("VEP_ASR_CONTRACT_SHA256", raising=False)
    runner = CliRunner()

    provision_help = runner.invoke(app, ["asr", "provision-worker", "--help"])
    assert provision_help.exit_code == 0
    assert "--profile" in provision_help.stdout
    assert "--repository" not in provision_help.stdout
    assert "--revision" not in provision_help.stdout

    provision = runner.invoke(
        app,
        [
            "asr",
            "provision-worker",
            "--profile",
            DEFAULT_ASR_PROFILE,
            "--output",
            str(tmp_path / "model"),
            "--json",
        ],
    )
    assert provision.exit_code != 0
    assert json.loads(provision.stdout)["error"]["code"] == "ASR_INTERNAL_COMMAND_REQUIRED"

    worker = runner.invoke(
        app,
        ["asr", "worker", "--request", str(tmp_path / "request.json"), "--json"],
    )
    assert worker.exit_code != 0
    assert json.loads(worker.stdout)["error"]["code"] == "ASR_INTERNAL_COMMAND_REQUIRED"


def test_inference_worker_rejects_request_profile_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    request = _worker_request().model_copy(
        update={
            "model_repository": "unreviewed/arbitrary-model",
        }
    )
    request_path = tmp_path / "request.json"
    request_path.write_bytes(pretty_json_bytes(request))
    monkeypatch.setattr(cli_module, "_require_asr_internal_runtime", lambda: tmp_path)

    result = CliRunner().invoke(
        app,
        ["asr", "worker", "--request", str(request_path), "--json"],
    )

    assert result.exit_code != 0
    assert json.loads(result.stdout)["error"]["code"] == "ASR_WORKER_PROFILE_MISMATCH"


def test_inference_worker_builds_truthful_provision_from_mounted_model(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _write_profile_model(model)
    profile = _profile()
    identity, count, size = model_payload_identity(model, profile)
    request = _worker_request().model_copy(
        update={
            "model_path": str(model),
            "model_storage_identity": identity,
            "model_file_count": count,
            "model_total_size_bytes": size,
        }
    )

    manifest = cli_module._verified_worker_provision(request, profile)

    assert manifest.model_storage_identity == identity
    assert manifest.file_count == count == 4
    assert manifest.total_size_bytes == size
    assert manifest.provisioned_at == request.model_provisioned_at


@pytest.mark.parametrize(
    ("stream", "limit_name"),
    [
        ("stdout", "_MAXIMUM_WORKER_OUTPUT_BYTES"),
        ("stderr", "_MAXIMUM_WORKER_ERROR_BYTES"),
    ],
)
def test_container_output_limits_are_enforced_while_process_is_running(
    monkeypatch: pytest.MonkeyPatch, stream: str, limit_name: str
) -> None:
    monkeypatch.setattr(container_module, limit_name, 1024)
    script = f"import sys; stream=sys.{stream}.buffer; stream.write(b'x' * 65536); stream.flush()"
    with pytest.raises(VepError) as overflow:
        container_module._run([sys.executable, "-c", script], code="ASR_TEST_FAILED")
    assert overflow.value.code == "ASR_TEST_FAILED"
    assert "byte limit" in str(overflow.value)


def test_model_payload_rejects_missing_required_file_and_unexpected_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "config.json").write_text("{}", encoding="utf-8")
    (incomplete / "model.bin").write_bytes(b"model")
    with pytest.raises(VepError) as missing:
        model_payload_identity(incomplete)
    assert missing.value.code == "ASR_MODEL_INVALID"

    complete = tmp_path / "complete"
    complete.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (complete / name).write_bytes(name.encode())
    unexpected = complete / "unexpected-link"
    unexpected.mkdir()
    original = provisioning_module.is_link_or_junction
    monkeypatch.setattr(
        provisioning_module,
        "is_link_or_junction",
        lambda path: path == unexpected or original(path),
    )
    with pytest.raises(VepError) as linked:
        model_payload_identity(complete)
    assert linked.value.code == "ASR_MODEL_INVALID"


@pytest.mark.parametrize(
    "missing_name",
    ["config.json", "model.bin", "tokenizer.json", "preprocessor_config.json"],
)
def test_large_v3_profile_rejects_each_missing_runtime_file(
    tmp_path: Path, missing_name: str
) -> None:
    model = tmp_path / "model"
    _write_profile_model(model)
    (model / missing_name).unlink()
    with pytest.raises(VepError) as invalid:
        model_payload_identity(model, _profile())
    assert invalid.value.code == "ASR_MODEL_INVALID"


@pytest.mark.parametrize("payload", ["{", '{"feature_size": 80}'])
def test_large_v3_profile_rejects_invalid_preprocessor_contract(
    tmp_path: Path, payload: str
) -> None:
    model = tmp_path / "model"
    _write_profile_model(model)
    (model / "preprocessor_config.json").write_text(payload, encoding="utf-8")
    with pytest.raises(VepError) as invalid:
        model_payload_identity(model, _profile())
    assert invalid.value.code == "ASR_MODEL_INVALID"


def test_model_identity_excludes_hugging_face_control_metadata(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.bin").write_bytes(b"model")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    control = model / ".cache" / "huggingface"
    control.mkdir(parents=True)
    marker = control / "download.json"
    marker.write_text('{"timestamp":"A"}', encoding="utf-8")
    first = model_payload_identity(model)
    marker.write_text('{"timestamp":"B"}', encoding="utf-8")
    second = model_payload_identity(model)
    assert first == second


def test_container_inference_is_network_disabled_and_model_mount_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    profile = _profile()
    model = model_storage_directory(tmp_path, DEFAULT_ASR_PROFILE, profile)
    model.mkdir(parents=True)
    _write_profile_model(model)
    model_identity, model_count, model_size = model_payload_identity(model)
    manifest = AsrModelProvisionManifest(
        profile=DEFAULT_ASR_PROFILE,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=model_identity,
        file_count=model_count,
        total_size_bytes=model_size,
        provisioned_at=datetime.now(UTC),
    )
    model_manifest_path(tmp_path, DEFAULT_ASR_PROFILE, profile).write_bytes(
        pretty_json_bytes(manifest)
    )
    commands: list[list[str]] = []
    gpu_preflight_calls = 0

    def gpu_preflight(*_args):
        nonlocal gpu_preflight_calls
        gpu_preflight_calls += 1
        return DeviceSelectionReason.CONTAINER_PASSTHROUGH_UNAVAILABLE

    monkeypatch.setattr(container_module, "_docker", lambda: "docker")
    monkeypatch.setattr(
        container_module,
        "_gpu_preflight_failure_reason",
        gpu_preflight,
    )
    monkeypatch.setattr(
        container_module, "_require_image_identity", lambda *_: "sha256:" + "a" * 64
    )
    monkeypatch.setattr(
        container_module,
        "resolve_asr_profile",
        lambda *_: (DEFAULT_ASR_PROFILE, _profile()),
    )

    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.ASR,
        name=profile.engine,
        version=profile.engine_version,
        runtime=profile.runtime,
        runtime_version=profile.runtime_version,
        model=profile.model_repository,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=manifest.model_storage_identity,
        device_policy="cpu",
        selection_reason=DeviceSelectionReason.EXPLICIT_CPU,
        selected_device="cpu",
        configured_min_free_vram_mib=profile.min_free_vram_mib,
        compute_type=profile.cpu_compute_type,
        container_image=profile.container_image,
        container_image_digest="sha256:" + "a" * 64,
        fallback=False,
    )

    class Result:
        stdout = AsrWorkerResponse(backend=backend, segments=[]).model_dump_json()
        stderr = ""

    def fake_run(command: list[str], *, code: str):
        assert code == "ASR_CONTAINER_TRANSCRIPTION_FAILED"
        commands.append(command)
        return Result()

    monkeypatch.setattr(container_module, "_run", fake_run)
    result = container_module.run_asr_worker(
        tmp_path,
        audio,
        (container_module.TranscriptInterval(start_ms=0, end_ms=1000),),
        TranscriptionConfig(asr_profile=DEFAULT_ASR_PROFILE, asr_device="cpu"),
    )
    assert result.backend == backend
    command = commands[0]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    assert any("dst=/model,readonly" in item for item in command)
    assert any("dst=/input/audio,readonly" in item for item in command)
    assert not any("/.local/jobs/" in item for item in command)
    assert "--gpus" not in command
    assert gpu_preflight_calls == 0


def test_host_nvidia_smi_failure_does_not_block_usable_container_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    profile = _profile()
    model = model_storage_directory(tmp_path, DEFAULT_ASR_PROFILE, profile)
    _write_profile_model(model)
    identity, count, size = model_payload_identity(model, profile)
    manifest = AsrModelProvisionManifest(
        profile=DEFAULT_ASR_PROFILE,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=identity,
        file_count=count,
        total_size_bytes=size,
        provisioned_at=datetime.now(UTC),
    )
    model_manifest_path(tmp_path, DEFAULT_ASR_PROFILE, profile).write_bytes(
        pretty_json_bytes(manifest)
    )
    monkeypatch.setattr(
        device_module,
        "probe_nvidia_gpus",
        lambda: pytest.fail("host nvidia-smi must not gate container GPU preflight"),
    )
    monkeypatch.setattr(container_module, "_docker", lambda: "docker")
    monkeypatch.setattr(container_module, "_gpu_preflight_failure_reason", lambda *_: None)
    monkeypatch.setattr(
        container_module, "_require_image_identity", lambda *_: "sha256:" + "a" * 64
    )
    gpu = GpuDevice(0, "gpu", 8192, 7000)
    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.ASR,
        name=profile.engine,
        version=profile.engine_version,
        runtime=profile.runtime,
        runtime_version=profile.runtime_version,
        model=profile.model_repository,
        model_repository=profile.model_repository,
        model_revision=profile.model_revision,
        model_storage_identity=identity,
        device_policy="auto",
        selection_reason=DeviceSelectionReason.AUTO_GPU_SELECTED,
        selected_device="cuda",
        gpu_index=gpu.index,
        gpu_name=gpu.name,
        total_vram_mib=gpu.total_vram_mib,
        free_vram_at_selection_mib=gpu.free_vram_mib,
        configured_min_free_vram_mib=profile.min_free_vram_mib,
        candidate_gpu_index=gpu.index,
        candidate_gpu_name=gpu.name,
        candidate_total_vram_mib=gpu.total_vram_mib,
        candidate_free_vram_mib=gpu.free_vram_mib,
        compute_type=profile.gpu_compute_type,
        container_image=profile.container_image,
        container_image_digest="sha256:" + "a" * 64,
        fallback=False,
    )
    commands: list[list[str]] = []

    def run(command: list[str], *, code: str):
        commands.append(command)
        return types.SimpleNamespace(
            stdout=AsrWorkerResponse(backend=backend, segments=[]).model_dump_json(),
            stderr="",
        )

    monkeypatch.setattr(container_module, "_run", run)
    response = container_module.run_asr_worker(
        tmp_path,
        audio,
        (TranscriptInterval(start_ms=0, end_ms=1000),),
        TranscriptionConfig(asr_profile=DEFAULT_ASR_PROFILE, asr_device="auto"),
    )
    assert response.backend.selected_device == "cuda"
    assert commands[0][commands[0].index("--gpus") + 1] == "all"


def test_public_gpu_preflight_rejects_invalid_cuda_index_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        container_module,
        "_docker",
        lambda: pytest.fail("invalid device policy must be rejected before Docker"),
    )

    with pytest.raises(VepError) as invalid:
        container_module.preflight_asr_gpu(tmp_path, device_policy="cuda:not-an-index")

    assert invalid.value.code == "ASR_DEVICE_INVALID"


def test_model_provisioning_worker_uses_only_noncanonical_staging_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(Path.cwd() / "config", tmp_path / "config")
    commands: list[list[str]] = []
    monkeypatch.setattr(container_module, "_docker", lambda: "docker")
    monkeypatch.setattr(
        container_module, "_require_image_identity", lambda *_: "sha256:" + "a" * 64
    )
    monkeypatch.setattr(
        container_module,
        "resolve_asr_profile",
        lambda *_: (DEFAULT_ASR_PROFILE, _profile()),
    )

    class Result:
        stdout = '{"ok": true}'
        stderr = ""

    def fake_run(command: list[str], *, code: str):
        assert code == "ASR_MODEL_PROVISION_FAILED"
        commands.append(command)
        mount = next(item for item in command if "dst=/output" in item)
        source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        model = source / "model"
        model.mkdir()
        _write_profile_model(model)
        return Result()

    monkeypatch.setattr(container_module, "_run", fake_run)
    container_module.run_provision_container(tmp_path, DEFAULT_ASR_PROFILE)

    command = commands[0]
    assert command[command.index("--pull") + 1] == "never"
    assert "--network" not in command
    assert any("dst=/output" in item for item in command)
    assert not any(".local/models" in item for item in command)
    assert "provision-worker" in command
