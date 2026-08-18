from __future__ import annotations

import json
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_authoring_pipeline import (
    _author_response,
    _evidence_ready_job,
    _publish_audit,
    _publish_initial_draft,
    _write_model,
)
from typer.testing import CliRunner

import video_evidence_pipeline.application as application_module
import video_evidence_pipeline.delivery.pipeline as delivery_pipeline_module
from video_evidence_pipeline.application import (
    _import_authoring_fixture,
    approve_job,
    export_job,
    job_status,
    prepare_authoring_job,
    validate_job,
)
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.delivery.pipeline import read_approval_records
from video_evidence_pipeline.domain.delivery import ReviewerIdentity, approval_record_fingerprint
from video_evidence_pipeline.domain.serialization import canonical_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.jsonl import exclusive_append_lock, read_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace


def _human_review_ready(tmp_path: Path, job_id: str):
    workspace, draft = _publish_initial_draft(tmp_path, job_id)
    audit = _publish_audit(
        tmp_path,
        workspace,
        context_id=f"{job_id}-clean-audit",
        material=False,
    )
    assert audit.human_review is not None
    return workspace, draft, audit


def _literal_marker_review_ready(tmp_path: Path, job_id: str):
    workspace = _evidence_ready_job(tmp_path, job_id)
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    response = _author_response(request, context_id=f"{job_id}-author")
    response = response.model_copy(
        update={"draft_markdown": f"{response.draft_markdown}\n[要確認]"}
    )
    response_path = tmp_path / f"{job_id}-author.json"
    _write_model(response_path, response)
    draft = _import_authoring_fixture(workspace.root, response_path)
    audit = _publish_audit(
        tmp_path,
        workspace,
        context_id=f"{job_id}-clean-audit",
        material=False,
    )
    assert audit.human_review is not None
    return workspace, draft, audit


def _approve(workspace: JobWorkspace, target: str, *, basis: str = "exact review"):
    return approve_job(
        workspace.root,
        approval_target_sha256=target,
        reviewer="reviewer-17",
        basis=basis,
        confirm=True,
        unresolved_notes=["non-blocking uncertainty"],
    )


def _clone_workspace(workspace: JobWorkspace, target: Path) -> JobWorkspace:
    source_repository = workspace.jobs_root.parent.parent
    shutil.copytree(source_repository, target)
    return JobWorkspace.open(target / ".local" / "jobs" / workspace.root.name)


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _recursive_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _recursive_keys(item)}
    return set()


def _transition_count(workspace: JobWorkspace, state: JobState) -> int:
    return [
        record.to_state for record in read_jsonl(workspace.transitions_jsonl, TransitionRecord)
    ].count(state)


def test_approval_gates_cli_privacy_exact_replay_and_markdown_export(
    tmp_path: Path,
) -> None:
    workspace, _draft, audit = _human_review_ready(tmp_path, "issue16-delivery-main")
    target = audit.human_review.approval_target_sha256

    invalid_calls = [
        {
            "approval_target_sha256": target,
            "reviewer": "reviewer",
            "basis": "basis",
            "confirm": False,
        },
        {
            "approval_target_sha256": "0" * 64,
            "reviewer": "reviewer",
            "basis": "basis",
            "confirm": True,
        },
        {
            "approval_target_sha256": target,
            "reviewer": "   ",
            "basis": "basis",
            "confirm": True,
        },
        {
            "approval_target_sha256": target,
            "reviewer": "reviewer",
            "basis": "   ",
            "confirm": True,
        },
    ]
    for arguments in invalid_calls:
        with pytest.raises(VepError):
            approve_job(workspace.root, **arguments)
        assert not workspace.approval_records_jsonl.exists()
        assert not workspace.approval_manifest_json.exists()

    bad_upstream = _clone_workspace(workspace, tmp_path / "bad-upstream")
    bad_human_path = bad_upstream.safe_path(
        audit.human_review.bundle_artifact.relative_storage_path
    )
    bad_human_path.write_bytes(bad_human_path.read_bytes() + b" ")
    with pytest.raises(VepError):
        _approve(bad_upstream, target)
    assert not bad_upstream.approval_records_jsonl.exists()
    assert not bad_upstream.approval_manifest_json.exists()

    runner = CliRunner()
    approved = runner.invoke(
        app,
        [
            "approve",
            str(workspace.root),
            "--approval-target-sha256",
            target,
            "--reviewer",
            "reviewer-17",
            "--basis",
            "exact review",
            "--unresolved-note",
            "non-blocking uncertainty",
            "--confirm",
            "--json",
        ],
    )
    assert approved.exit_code == 0, approved.stdout
    approved_payload = json.loads(approved.stdout)
    assert approved_payload["state"] == JobState.HUMAN_APPROVED.value
    assert approved_payload["reviewer_assertion_method"] == "cli_asserted"
    assert approved_payload["reviewer_cryptographically_verified"] is False
    assert not {
        "basis",
        "unresolved_notes",
        "draft_markdown",
        "content",
    } & _recursive_keys(approved_payload)
    assert "non-blocking uncertainty" not in approved.stdout

    first_record = read_approval_records(workspace)[0]
    assert first_record.confirmed_by_user is True
    assert first_record.unresolved_notes == ["non-blocking uncertainty"]
    replay = _approve(workspace, target)
    assert replay.reused is True
    assert replay.record.approved_at == first_record.approved_at
    assert _transition_count(workspace, JobState.HUMAN_APPROVED) == 1
    with pytest.raises(VepError) as mismatch:
        _approve(workspace, target, basis="different basis")
    assert mismatch.value.code == "APPROVAL_REPLAY_MISMATCH"

    unsafe_attempt = workspace.delivery_staging_root / ("b" * 32)
    (unsafe_attempt / "nested").mkdir(parents=True)
    with pytest.raises(VepError) as unsafe_staging:
        export_job(workspace.root)
    assert unsafe_staging.value.code == "EXPORT_STAGING_INVALID"
    shutil.rmtree(unsafe_attempt)
    stale_attempt = workspace.delivery_staging_root / ("a" * 32)
    stale_attempt.mkdir()
    (stale_attempt / "final.md").write_bytes(b"partial")

    exported = export_job(workspace.root)
    assert not stale_attempt.exists()
    draft_bytes = workspace.safe_path(
        exported.manifest.approved_draft_artifact.relative_storage_path
    ).read_bytes()
    final_bytes = workspace.safe_path(
        exported.manifest.final_artifact.relative_storage_path
    ).read_bytes()
    assert final_bytes == draft_bytes
    assert exported.manifest.byte_exact is True
    assert exported.package_manifest.created_at >= first_record.approved_at
    assert export_job(workspace.root).reused is True
    assert _transition_count(workspace, JobState.EXPORTED) == 1
    assert validate_job(workspace.root)["state"] == JobState.EXPORTED.value
    status, exit_code = job_status(workspace.root)
    assert exit_code == 0
    assert status["approval_integrity"]["status"] == "valid"
    assert status["export_integrity"]["status"] == "valid"
    assert "unresolved_notes" not in _recursive_keys(status)

    export_cli = runner.invoke(app, ["export", str(workspace.root), "--json"])
    assert export_cli.exit_code == 0, export_cli.stdout
    export_payload = json.loads(export_cli.stdout)
    assert export_payload["state"] == JobState.EXPORTED.value
    assert not {
        "basis",
        "unresolved_notes",
        "draft_markdown",
        "content",
    } & _recursive_keys(export_payload)

    replay_cli = runner.invoke(
        app,
        [
            "approve",
            str(workspace.root),
            "--approval-target-sha256",
            target,
            "--reviewer",
            "reviewer-17",
            "--basis",
            "exact review",
            "--unresolved-note",
            "non-blocking uncertainty",
            "--confirm",
            "--json",
        ],
    )
    assert replay_cli.exit_code == 0, replay_cli.stdout
    assert json.loads(replay_cli.stdout)["state"] == JobState.EXPORTED.value

    package_tamper_clone = _clone_workspace(workspace, tmp_path / "package-tamper")
    package_path = package_tamper_clone.safe_path(
        f"exports/markdown/{first_record.record_sha256}/package-manifest.json"
    )
    package_path.write_bytes(package_path.read_bytes() + b" ")
    with pytest.raises(VepError) as package_tamper:
        export_job(package_tamper_clone.root)
    assert package_tamper.value.code == "EXPORT_BUNDLE_INVALID"
    assert _transition_count(package_tamper_clone, JobState.EXPORTED) == 1

    final_path = workspace.safe_path(exported.manifest.final_artifact.relative_storage_path)
    final_path.write_bytes(final_path.read_bytes() + b"tamper")
    with pytest.raises(VepError) as export_tamper:
        export_job(workspace.root)
    assert export_tamper.value.code == "EXPORT_BUNDLE_INVALID"
    assert _transition_count(workspace, JobState.EXPORTED) == 1


def test_approval_rejects_literal_marker_and_export_rechecks_legacy_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, draft, audit = _literal_marker_review_ready(tmp_path, "issue16-marker")
    target = audit.human_review.approval_target_sha256
    current_workspace, job, _transitions, state = application_module._load_context(workspace.root)
    _fixed, verified_draft, verified_audit, human_review = application_module._approval_upstream(
        current_workspace,
        job,
        state,
        stale_code=False,
    )
    assert human_review.bundle.confirmation_spans == []
    transition_before = workspace.transitions_jsonl.read_bytes()
    staging_before = tuple(sorted(path.name for path in workspace.delivery_staging_root.iterdir()))

    with pytest.raises(VepError) as approval_error:
        approve_job(
            workspace.root,
            approval_target_sha256=target,
            reviewer="reviewer-17",
            basis="exact review",
            confirm=True,
            unresolved_notes=["non-blocking uncertainty"],
        )
    assert approval_error.value.code == "APPROVAL_CONFIRMATION_UNRESOLVED"
    assert application_module._load_context(workspace.root)[3] is JobState.HUMAN_REVIEW_READY
    assert _transition_count(workspace, JobState.HUMAN_APPROVED) == 0
    assert _transition_count(workspace, JobState.EXPORTED) == 0
    assert workspace.transitions_jsonl.read_bytes() == transition_before
    assert (
        tuple(sorted(path.name for path in workspace.delivery_staging_root.iterdir()))
        == staging_before
    )
    assert not workspace.approval_records_jsonl.exists()
    assert not workspace.approval_stage_manifest_json.exists()
    assert not workspace.approval_manifest_json.exists()
    assert not workspace.export_stage_manifest_json.exists()
    assert not workspace.export_manifest_json.exists()
    assert not list((workspace.root / "exports" / "markdown").iterdir())
    assert (
        "[要確認]".encode()
        in workspace.safe_path(draft.manifest.draft_artifact.relative_storage_path).read_bytes()
    )

    with monkeypatch.context() as legacy:
        legacy.setattr(
            delivery_pipeline_module,
            "ensure_approval_confirmation_resolved",
            lambda *_args: None,
        )
        legacy_approval = delivery_pipeline_module.approve_or_recover(
            workspace,
            job,
            verified_draft,
            verified_audit,
            human_review,
            reviewer=ReviewerIdentity(identity="legacy-reviewer"),
            basis="legacy approval record",
            unresolved_notes=["non-blocking uncertainty"],
            approved_at=datetime.now(UTC),
        )
    assert legacy_approval.record.unresolved_notes == ["non-blocking uncertainty"]
    with exclusive_append_lock(workspace.transitions_jsonl):
        current_workspace, job, _transitions, state = application_module._load_context(
            workspace.root
        )
        assert state is JobState.HUMAN_REVIEW_READY
        application_module._append_transition_locked(
            current_workspace,
            job,
            from_state=JobState.HUMAN_REVIEW_READY,
            to_state=JobState.HUMAN_APPROVED,
            trigger="legacy.approve",
        )

    export_transition_before = workspace.transitions_jsonl.read_bytes()
    export_staging_before = tuple(
        sorted(path.name for path in workspace.delivery_staging_root.iterdir())
    )
    with pytest.raises(VepError) as export_error:
        export_job(workspace.root)
    assert export_error.value.code == "APPROVAL_STALE"
    assert application_module._load_context(workspace.root)[3] is JobState.HUMAN_APPROVED
    assert _transition_count(workspace, JobState.HUMAN_APPROVED) == 1
    assert _transition_count(workspace, JobState.EXPORTED) == 0
    assert workspace.transitions_jsonl.read_bytes() == export_transition_before
    assert (
        tuple(sorted(path.name for path in workspace.delivery_staging_root.iterdir()))
        == export_staging_before
    )
    assert not workspace.export_stage_manifest_json.exists()
    assert not workspace.export_manifest_json.exists()
    assert not list((workspace.root / "exports" / "markdown").iterdir())
    with pytest.raises(VepError) as validation_error:
        validate_job(workspace.root)
    assert validation_error.value.code == "APPROVAL_STALE"
    status, exit_code = job_status(workspace.root)
    assert exit_code == 5
    assert status["stored_state"] == JobState.HUMAN_APPROVED.value
    assert status["approval_integrity"]["error_code"] == "APPROVAL_STALE"


def test_approval_ledger_tampering_is_bounded_and_approval_specific(tmp_path: Path) -> None:
    workspace, _draft, audit = _human_review_ready(tmp_path, "issue16-ledger")
    target = audit.human_review.approval_target_sha256
    _approve(workspace, target)
    original = workspace.approval_records_jsonl.read_bytes()

    variants: dict[str, bytes] = {
        "append": original + original,
        "truncated": original[:-1],
    }
    payload = json.loads(original.decode("utf-8"))
    variants["noncanonical"] = (
        json.dumps(payload, ensure_ascii=False, sort_keys=False).encode("utf-8") + b"\n"
    )
    variants["duplicate-key"] = original.replace(b"{", b'{"schema_version":"1.0",', 1)
    variants["self"] = canonical_json_bytes({**payload, "record_sha256": "f" * 64}) + b"\n"
    chained = {**payload, "sequence": 2, "previous_record_sha256": "a" * 64}
    chained["record_sha256"] = approval_record_fingerprint(chained)
    variants["chain"] = canonical_json_bytes(chained) + b"\n"

    for name, contents in variants.items():
        clone = _clone_workspace(workspace, tmp_path / name)
        clone.approval_records_jsonl.write_bytes(contents)
        if name in {"noncanonical", "self"}:
            assert len(read_approval_records(clone)) == 1
            if name == "self":
                with pytest.raises(VepError):
                    validate_job(clone.root, deep=True)
            continue
        with pytest.raises(VepError) as captured:
            read_approval_records(clone)
        assert captured.value.code == "APPROVAL_LEDGER_INVALID"


def test_normal_approval_validation_does_not_rehash_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _draft, audit = _human_review_ready(tmp_path, "issue16-normal-hash-count")
    _approve(workspace, audit.human_review.approval_target_sha256)
    calls = 0

    def counted(path: Path) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError(f"normal approval validation hashed {path}")

    monkeypatch.setattr(delivery_pipeline_module, "sha256_file", counted)
    assert validate_job(workspace.root)["valid"] is True
    assert calls == 0


def test_export_rejects_stale_upstream_and_unsupported_format_without_transition(
    tmp_path: Path,
) -> None:
    workspace, _draft, audit = _human_review_ready(tmp_path, "issue16-stale")
    target = audit.human_review.approval_target_sha256
    approval = _approve(workspace, target)

    clones: list[tuple[str, JobWorkspace, Path]] = []
    draft_clone = _clone_workspace(workspace, tmp_path / "draft")
    clones.append(
        (
            "draft",
            draft_clone,
            draft_clone.safe_path(approval.record.approved_draft_artifact.relative_storage_path),
        )
    )
    human_clone = _clone_workspace(workspace, tmp_path / "human")
    clones.append(
        (
            "human",
            human_clone,
            human_clone.safe_path(approval.record.human_review_artifact.relative_storage_path),
        )
    )
    audit_clone = _clone_workspace(workspace, tmp_path / "audit")
    clones.append(
        (
            "audit",
            audit_clone,
            audit_clone.safe_path(
                approval.record.current_audit_findings_artifact.relative_storage_path
            ),
        )
    )
    approval_clone = _clone_workspace(workspace, tmp_path / "approval")
    clones.append(("approval", approval_clone, approval_clone.approval_records_jsonl))
    approval_manifest_clone = _clone_workspace(workspace, tmp_path / "approval-manifest")
    clones.append(
        (
            "approval-manifest",
            approval_manifest_clone,
            approval_manifest_clone.approval_manifest_json,
        )
    )

    for _name, clone, target_path in clones:
        target_path.write_bytes(target_path.read_bytes() + b" ")
        with pytest.raises(VepError) as captured:
            export_job(clone.root)
        assert captured.value.code == "APPROVAL_STALE"
        assert _transition_count(clone, JobState.EXPORTED) == 0
        assert not clone.export_manifest_json.exists()
        with pytest.raises(VepError) as validate_stale:
            validate_job(clone.root)
        assert validate_stale.value.code == "APPROVAL_STALE"
        stale_status, stale_exit = job_status(clone.root)
        assert stale_status["stored_state"] == JobState.HUMAN_APPROVED.value
        assert stale_status["approval_integrity"]["error_code"] == "APPROVAL_STALE"
        assert stale_exit == 5

    job_clone = _clone_workspace(workspace, tmp_path / "job")
    job_text = job_clone.job_yaml.read_text(encoding="utf-8")
    job_clone.job_yaml.write_text(
        job_text.replace("language: en", "language: ja"), encoding="utf-8"
    )
    with pytest.raises(VepError) as job_stale:
        export_job(job_clone.root)
    assert job_stale.value.code == "APPROVAL_STALE"
    assert _transition_count(job_clone, JobState.EXPORTED) == 0

    format_clone = _clone_workspace(workspace, tmp_path / "format")
    format_text = format_clone.job_yaml.read_text(encoding="utf-8")
    format_clone.job_yaml.write_text(
        format_text.replace("format: markdown", "format: pdf"), encoding="utf-8"
    )
    with pytest.raises(VepError) as unsupported:
        export_job(format_clone.root)
    assert unsupported.value.code == "EXPORT_FORMAT_NOT_IMPLEMENTED"
    assert _transition_count(format_clone, JobState.EXPORTED) == 0


def test_approval_and_export_crash_boundaries_recover_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _draft, audit = _human_review_ready(tmp_path, "issue16-crashes")
    target = audit.human_review.approval_target_sha256
    original_write = delivery_pipeline_module._write_or_verify_model

    for name, target_getter in (
        ("approval-stage", lambda clone: clone.approval_stage_manifest_json),
        ("approval-primary", lambda clone: clone.approval_manifest_json),
    ):
        clone = _clone_workspace(workspace, tmp_path / name)
        crash_target = target_getter(clone)
        crashed = False

        def crash_once(path, *args, _target=crash_target, **kwargs):
            nonlocal crashed
            if path == _target and not crashed:
                crashed = True
                raise RuntimeError("synthetic approval crash")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(delivery_pipeline_module, "_write_or_verify_model", crash_once)
        with pytest.raises(RuntimeError, match="approval crash"):
            _approve(clone, target)
        record_time = read_approval_records(clone)[0].approved_at
        assert _transition_count(clone, JobState.HUMAN_APPROVED) == 0
        monkeypatch.setattr(delivery_pipeline_module, "_write_or_verify_model", original_write)
        recovered = _approve(clone, target)
        assert recovered.record.approved_at == record_time
        assert _transition_count(clone, JobState.HUMAN_APPROVED) == 1

    transition_clone = _clone_workspace(workspace, tmp_path / "approval-transition")
    original_append = application_module._append_transition_locked
    crashed_transition = False

    def crash_approval_transition(*args, **kwargs):
        nonlocal crashed_transition
        if kwargs.get("to_state") is JobState.HUMAN_APPROVED and not crashed_transition:
            crashed_transition = True
            raise RuntimeError("synthetic approval transition crash")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(application_module, "_append_transition_locked", crash_approval_transition)
    with pytest.raises(RuntimeError, match="approval transition crash"):
        _approve(transition_clone, target)
    monkeypatch.setattr(application_module, "_append_transition_locked", original_append)
    _approve(transition_clone, target)
    assert _transition_count(transition_clone, JobState.HUMAN_APPROVED) == 1

    approved_base = _clone_workspace(workspace, tmp_path / "approved-base")
    _approve(approved_base, target)
    original_copy = delivery_pipeline_module._MARKDOWN_ADAPTER.copy_exact
    original_publish = delivery_pipeline_module._publish_exact

    final_clone = _clone_workspace(approved_base, tmp_path / "export-final")
    final_target = final_clone.safe_path(
        f"exports/markdown/{read_approval_records(final_clone)[0].record_sha256}/final.md"
    )
    crashed_final = False

    def crash_after_final(source, destination, expected):
        nonlocal crashed_final
        original_copy(source, destination, expected)
        if destination == final_target and not crashed_final:
            crashed_final = True
            raise RuntimeError("synthetic final crash")

    monkeypatch.setattr(delivery_pipeline_module._MARKDOWN_ADAPTER, "copy_exact", crash_after_final)
    with pytest.raises(RuntimeError, match="final crash"):
        export_job(final_clone.root)
    monkeypatch.setattr(delivery_pipeline_module._MARKDOWN_ADAPTER, "copy_exact", original_copy)
    recovered_final = export_job(final_clone.root)
    assert recovered_final.package_manifest.created_at >= recovered_final.manifest.created_at
    assert _transition_count(final_clone, JobState.EXPORTED) == 1

    package_clone = _clone_workspace(approved_base, tmp_path / "export-package")
    package_target = package_clone.safe_path(
        f"exports/markdown/{read_approval_records(package_clone)[0].record_sha256}/package-manifest.json"
    )
    crashed_package = False

    def crash_after_package(source, destination, expected):
        nonlocal crashed_package
        original_publish(source, destination, expected)
        if destination == package_target and not crashed_package:
            crashed_package = True
            raise RuntimeError("synthetic package crash")

    monkeypatch.setattr(delivery_pipeline_module, "_publish_exact", crash_after_package)
    with pytest.raises(RuntimeError, match="package crash"):
        export_job(package_clone.root)
    monkeypatch.setattr(delivery_pipeline_module, "_publish_exact", original_publish)
    export_job(package_clone.root)
    assert _transition_count(package_clone, JobState.EXPORTED) == 1

    primary_clone = _clone_workspace(approved_base, tmp_path / "export-primary")
    primary_target = primary_clone.export_manifest_json
    crashed_primary = False

    def crash_export_primary(path, *args, **kwargs):
        nonlocal crashed_primary
        if path == primary_target and not crashed_primary:
            crashed_primary = True
            raise RuntimeError("synthetic export primary crash")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(delivery_pipeline_module, "_write_or_verify_model", crash_export_primary)
    with pytest.raises(RuntimeError, match="export primary crash"):
        export_job(primary_clone.root)
    monkeypatch.setattr(delivery_pipeline_module, "_write_or_verify_model", original_write)
    export_job(primary_clone.root)
    assert _transition_count(primary_clone, JobState.EXPORTED) == 1

    export_transition_clone = _clone_workspace(approved_base, tmp_path / "export-transition")
    crashed_export_transition = False

    def crash_export_transition(*args, **kwargs):
        nonlocal crashed_export_transition
        if kwargs.get("to_state") is JobState.EXPORTED and not crashed_export_transition:
            crashed_export_transition = True
            raise RuntimeError("synthetic export transition crash")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(application_module, "_append_transition_locked", crash_export_transition)
    with pytest.raises(RuntimeError, match="export transition crash"):
        export_job(export_transition_clone.root)
    monkeypatch.setattr(application_module, "_append_transition_locked", original_append)
    export_job(export_transition_clone.root)
    assert _transition_count(export_transition_clone, JobState.EXPORTED) == 1


def test_concurrent_identical_approval_has_one_record_and_transition(tmp_path: Path) -> None:
    workspace, _draft, audit = _human_review_ready(tmp_path, "issue16-concurrent")
    target = audit.human_review.approval_target_sha256
    barrier = threading.Barrier(2)
    results: list[object] = []

    def approve() -> None:
        barrier.wait()
        try:
            results.append(_approve(workspace, target))
        except VepError as exc:
            results.append(exc)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert any(not isinstance(item, VepError) for item in results)
    assert all(
        not isinstance(item, VepError) or item.code == "TRANSITION_LOG_BUSY" for item in results
    )
    assert len(read_approval_records(workspace)) == 1
    assert _transition_count(workspace, JobState.HUMAN_APPROVED) == 1

    export_barrier = threading.Barrier(2)
    export_results: list[object] = []

    def export() -> None:
        export_barrier.wait()
        try:
            export_results.append(export_job(workspace.root))
        except VepError as exc:
            export_results.append(exc)

    export_threads = [threading.Thread(target=export) for _ in range(2)]
    for thread in export_threads:
        thread.start()
    for thread in export_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert any(not isinstance(item, VepError) for item in export_results)
    assert all(
        not isinstance(item, VepError) or item.code == "TRANSITION_LOG_BUSY"
        for item in export_results
    )
    assert _transition_count(workspace, JobState.EXPORTED) == 1
