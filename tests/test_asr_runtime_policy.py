from __future__ import annotations

import re
import shutil
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.domain.transcription import TranscriptionConfig
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.transcription import container as container_module
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
from video_evidence_pipeline.transcription.provisioning import provision_model


def _profile():
    selected, profile = resolve_asr_profile(Path.cwd(), DEFAULT_ASR_PROFILE)
    assert selected == DEFAULT_ASR_PROFILE
    return profile


def _caps(
    *gpus: GpuDevice,
    passthrough: bool = True,
    cuda: bool = True,
    compute: frozenset[str] = frozenset({"float16"}),
) -> RuntimeCapabilities:
    return RuntimeCapabilities(tuple(gpus), passthrough, cuda, compute)


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
    assert all(re.fullmatch(r"[a-z0-9_.-]+==[^=\s]+", item) for item in requirements)
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


def test_only_gpu_resource_failures_are_fallback_eligible() -> None:
    assert is_gpu_resource_error(RuntimeError("CUDA out of memory"))
    assert is_gpu_resource_error(RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"))
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
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.bin").write_bytes(b"model")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
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
        (local_dir / "config.json").write_text("{}", encoding="utf-8")
        (local_dir / "model.bin").write_bytes(b"model")
        (local_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

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
    assert not list(model.parent.parent.glob(".provision-*"))


def test_container_inference_is_network_disabled_and_model_mount_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = JobWorkspace.create(tmp_path, "container-policy")
    (tmp_path / "config").mkdir()
    (tmp_path / ".local" / "models").mkdir(parents=True)
    commands: list[list[str]] = []

    monkeypatch.setattr(container_module, "_docker", lambda: "docker")
    monkeypatch.setattr(container_module, "_gpu_passthrough_usable", lambda *_: False)
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

    def fake_run(command: list[str], *, code: str):
        assert code == "ASR_CONTAINER_TRANSCRIPTION_FAILED"
        commands.append(command)
        return Result()

    monkeypatch.setattr(container_module, "_run", fake_run)
    result = container_module.run_transcription_container(
        workspace.root,
        TranscriptionConfig(asr_profile=DEFAULT_ASR_PROFILE),
    )
    assert result == '{"ok": true}'
    command = commands[0]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    assert any("dst=/workspace/.local/models,readonly" in item for item in command)
    assert "--gpus" not in command


def test_model_provisioning_is_explicit_and_uses_writable_persistent_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config").mkdir()
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
        stdout = '{"profile": "whisper-large-v3"}'

    def fake_run(command: list[str], *, code: str):
        assert code == "ASR_MODEL_PROVISION_FAILED"
        commands.append(command)
        return Result()

    monkeypatch.setattr(container_module, "_run", fake_run)
    container_module.run_provision_container(tmp_path, DEFAULT_ASR_PROFILE)

    command = commands[0]
    assert command[command.index("--pull") + 1] == "never"
    assert "--network" not in command
    assert any("dst=/workspace/.local/models" in item for item in command)
    assert not any("dst=/workspace/.local/models,readonly" in item for item in command)
    assert command[-7:] == [
        "asr",
        "model-provision",
        "--repository-root",
        "/workspace",
        "--profile",
        DEFAULT_ASR_PROFILE,
        "--json",
    ]
