from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from video_evidence_pipeline.application import initialize_job, job_status
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.runtime_context import (
    DataContext,
    ResourceContext,
    RuntimeContext,
    current_runtime,
    model_root,
    use_runtime,
)
from video_evidence_pipeline.semantic.skills import load_skill_snapshot
from video_evidence_pipeline.storage.layout import JobWorkspace

ROOT = Path(__file__).resolve().parents[1]


def context_for(tmp_path: Path) -> RuntimeContext:
    data = tmp_path / "data"
    bindings = tmp_path / "bindings"
    data.mkdir()
    bindings.mkdir()
    return RuntimeContext("dev-r1", ResourceContext(ROOT), DataContext(data, bindings))


def test_installed_job_uses_explicit_roots_and_external_binding(tmp_path):
    context = context_for(tmp_path)
    with use_runtime(context):
        workspace, _job = initialize_job(
            tmp_path / "unrelated-cwd",
            job_id="new-job",
            source_kind=SourceKind.LOCAL_FILE,
            source=str(tmp_path / "public-fixture.txt"),
            task_type="summary",
            language="ja",
            output_format="markdown",
        )
        assert workspace.root == context.data.jobs_root / "new-job"
        before = workspace.job_yaml.read_bytes()
        assert JobWorkspace.open(workspace.root).resources_root == ROOT
        assert job_status(workspace.root)[0]["stored_state"] == "CREATED"
        snapshot = load_skill_snapshot(
            workspace, "draft-video-summary", allowed=frozenset({"draft-video-summary"})
        )
        assert snapshot.lifecycle == "active"
        assert model_root(ROOT) == context.data.models_root
        binding = json.loads(context.binding_path(workspace.root).read_text())
        assert binding["release_id"] == "dev-r1"
        assert workspace.job_yaml.read_bytes() == before
    assert current_runtime() is None
    assert not (tmp_path / "unrelated-cwd").exists()


def test_update_and_rollback_cannot_open_job_with_wrong_runtime(tmp_path):
    context = context_for(tmp_path)
    with use_runtime(context):
        workspace = JobWorkspace.create(ROOT, "pinned")
        sentinel = workspace.root / "approval/sentinel.json"
        sentinel.write_bytes(b'{"synthetic-approval":true}\n')
    before = {p: p.read_bytes() for p in workspace.root.rglob("*") if p.is_file()}
    with use_runtime(replace(context, release_id="dev-r2")):
        with pytest.raises(VepError, match="pinned runtime") as raised:
            JobWorkspace.open(workspace.root)
        assert raised.value.code == "JOB_RUNTIME_MISMATCH"
    with use_runtime(context):
        assert JobWorkspace.open(workspace.root).root == workspace.root
    assert all(p.read_bytes() == content for p, content in before.items())


def test_legacy_source_job_is_unchanged_and_installed_mode_refuses_unknown_binding(tmp_path):
    repository = tmp_path / "legacy-source"
    repository.mkdir()
    workspace = JobWorkspace.create(repository, "legacy")
    (workspace.root / "sentinel").write_bytes(b"immutable synthetic source\n")
    before = {p: p.read_bytes() for p in workspace.root.rglob("*") if p.is_file()}
    assert JobWorkspace.open(workspace.root).resources_root == repository
    context = context_for(tmp_path)
    with use_runtime(context):
        with pytest.raises(VepError) as raised:
            JobWorkspace.open(workspace.root)
        assert raised.value.code == "JOB_RUNTIME_BINDING_REQUIRED"
    assert JobWorkspace.open(workspace.root).resources_root == repository
    assert all(p.read_bytes() == content for p, content in before.items())
    assert not any(context.data.bindings_root.iterdir())


def test_context_rejects_overlapping_roots_and_resets_after_exception(tmp_path):
    context = context_for(tmp_path)
    with pytest.raises(RuntimeError), use_runtime(context):
        raise RuntimeError("synthetic failure")
    assert current_runtime() is None
    invalid = replace(context, resources=ResourceContext(tmp_path))
    with pytest.raises(VepError) as raised, use_runtime(invalid):
        pass
    assert raised.value.code == "RUNTIME_CONTEXT_INVALID"


def test_installed_asr_gate_precedes_docker_or_network(tmp_path, monkeypatch):
    from video_evidence_pipeline.transcription import container

    def forbidden():
        raise AssertionError("installed prototype must not probe Docker")

    monkeypatch.setattr(container, "_docker", forbidden)
    with use_runtime(context_for(tmp_path)):
        for operation in (
            container.build_asr_image,
            container.run_provision_container,
            container.smoke_asr_image,
            container.preflight_asr_gpu,
        ):
            with pytest.raises(VepError) as raised:
                operation(ROOT)
            assert raised.value.code == "ASR_INSTALLED_COMPONENT_NOT_READY"


def test_retry_reuses_only_identical_orphan_binding_and_conflict_does_not_create_tree(tmp_path):
    import shutil

    context = context_for(tmp_path)
    with use_runtime(context):
        workspace = JobWorkspace.create(ROOT, "retry")
        binding = context.binding_path(workspace.root).read_bytes()
        shutil.rmtree(workspace.root)  # synthetic failed-recovery cleanup
        recreated = JobWorkspace.create(ROOT, "retry")
        assert recreated.root == workspace.root
        assert context.binding_path(workspace.root).read_bytes() == binding
        shutil.rmtree(recreated.root)
    with use_runtime(replace(context, release_id="other-runtime")):
        with pytest.raises(VepError) as raised:
            JobWorkspace.create(ROOT, "retry")
        assert raised.value.code == "JOB_RUNTIME_MISMATCH"
    assert not workspace.root.exists()
    assert context.binding_path(workspace.root).read_bytes() == binding


def test_installed_create_rejects_data_root_override_before_write(tmp_path):
    context = context_for(tmp_path)
    outside = tmp_path / "outside/jobs"
    with use_runtime(context), pytest.raises(VepError) as raised:
        JobWorkspace.create(ROOT, "escape", jobs_root=outside)
    assert raised.value.code == "WORKSPACE_LAYOUT_INVALID"
    assert not outside.parent.exists()
    assert not any(context.data.bindings_root.iterdir())


def test_installed_export_refuses_binding_and_state_roots_without_writes(tmp_path):
    from video_evidence_pipeline.delivery.study_bundle import publish_study_bundle

    context = context_for(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    context = replace(context, protected_roots=(state,))
    with use_runtime(context):
        workspace = JobWorkspace.create(ROOT, "export-guard")
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        for destination in (context.data.bindings_root / "archive", state / "archive"):
            with pytest.raises(VepError) as raised:
                publish_study_bundle(workspace, None, None, None, None, None, destination)
            assert raised.value.code == "STUDY_BUNDLE_DESTINATION_INVALID"
        assert all(p.read_bytes() == content for p, content in before.items())
        assert not list(tmp_path.rglob("archive"))


def test_installed_recovery_real_path_and_model_storage_use_data_only(tmp_path, monkeypatch):
    from test_issue33_recovery import _transcript_parent

    from video_evidence_pipeline import application
    from video_evidence_pipeline.application import recover_job
    from video_evidence_pipeline.transcription.provisioning import _ensure_profile_storage

    context = context_for(tmp_path)
    with use_runtime(context):
        parent = _transcript_parent(tmp_path, "installed-parent")
        before = {p: p.read_bytes() for p in parent.root.rglob("*") if p.is_file()}
        original = application.atomic_copy_exclusive

        def broken_copy(*args, **kwargs):
            raise OSError("synthetic copy failure")

        monkeypatch.setattr(application, "atomic_copy_exclusive", broken_copy)
        with pytest.raises((OSError, VepError)):
            recover_job(parent.root, "installed-child", confirm=True, basis="synthetic retry")
        monkeypatch.setattr(application, "atomic_copy_exclusive", original)
        result = recover_job(parent.root, "installed-child", confirm=True, basis="synthetic retry")
        assert result.workspace.jobs_root.resolve() == context.data.jobs_root.resolve()
        assert result.workspace.resources_root == ROOT
        context.require_binding(result.workspace.root)
        assert all(p.read_bytes() == content for p, content in before.items())
        model_path = _ensure_profile_storage(ROOT, "whisper-large-v3")
        assert model_path == context.data.models_root / "asr/whisper-large-v3"
