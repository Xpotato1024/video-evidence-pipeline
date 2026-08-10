"""Crash-recoverable human approval and byte-exact Markdown delivery."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline.authoring.pipeline import AuditResult, DraftResult, HumanReviewResult
from video_evidence_pipeline.delivery.markdown import MarkdownByteExactAdapter
from video_evidence_pipeline.domain.artifacts import ArtifactRef, ProducerIdentity, ProducerKind
from video_evidence_pipeline.domain.authoring import FixedEvidenceBundle
from video_evidence_pipeline.domain.delivery import (
    ApprovalManifest,
    ApprovalRecord,
    ExportManifest,
    ExportPackageManifest,
    ReviewerIdentity,
    approval_intent_fingerprint,
    approval_manifest_fingerprint,
    approval_record_fingerprint,
    export_manifest_fingerprint,
    export_package_fingerprint,
)
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import JobSpec, job_fingerprint
from video_evidence_pipeline.domain.manifests import StageManifest, StageStatus
from video_evidence_pipeline.domain.serialization import canonical_json_bytes, pretty_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.errors import VepError, integrity_error, io_error, state_error
from video_evidence_pipeline.storage.atomic import atomic_copy_exclusive, atomic_write_new_bytes
from video_evidence_pipeline.storage.jsonl import append_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import ensure_private_directory

_MAX_APPROVAL_LEDGER_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_DRAFT_BYTES = 4 * 1024 * 1024
FORBIDDEN_APPROVAL_MARKERS: tuple[str, ...] = ("[要確認]",)
_FORBIDDEN_APPROVAL_MARKER_BYTES = tuple(
    marker.encode("utf-8") for marker in FORBIDDEN_APPROVAL_MARKERS
)
_APPROVAL_PRODUCER = ProducerIdentity(
    kind=ProducerKind.TOOL,
    name="video-evidence-pipeline-approval",
    version=MarkdownByteExactAdapter.identity.version,
)
_MARKDOWN_ADAPTER = MarkdownByteExactAdapter()
_EXPORT_ADAPTER = _MARKDOWN_ADAPTER.identity
_EXPORT_PRODUCER = _MARKDOWN_ADAPTER.producer
_ATTEMPT_NAME = re.compile(r"^[0-9a-f]{32}$")
_MAXIMUM_STALE_ATTEMPTS = 256
_MAXIMUM_ATTEMPT_ENTRIES = 32


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    record: ApprovalRecord
    manifest: ApprovalManifest
    stage_manifest: StageManifest
    reused: bool


@dataclass(frozen=True, slots=True)
class ExportResult:
    manifest: ExportManifest
    package_manifest: ExportPackageManifest
    stage_manifest: StageManifest
    reused: bool


def _is_link_or_mount(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or is_junction() or path.is_mount()


def _remove_delivery_attempt(root: Path, attempt: Path) -> None:
    try:
        if attempt.parent.resolve(strict=True) != root.resolve(strict=True):
            raise integrity_error("EXPORT_STAGING_INVALID", "export staging escapes its root")
    except OSError as exc:
        raise io_error("EXPORT_STAGING_INVALID", "export staging could not be resolved") from exc
    if (
        _is_link_or_mount(attempt)
        or not attempt.is_dir()
        or not _ATTEMPT_NAME.fullmatch(attempt.name)
    ):
        raise integrity_error("EXPORT_STAGING_INVALID", "export staging entry is unsafe")
    entries = list(attempt.iterdir())
    if len(entries) > _MAXIMUM_ATTEMPT_ENTRIES:
        raise integrity_error("EXPORT_STAGING_INVALID", "export staging is unexpectedly large")
    if any(_is_link_or_mount(entry) or entry.is_dir() for entry in entries):
        raise integrity_error(
            "EXPORT_STAGING_INVALID", "export staging contains an unsafe nested entry"
        )
    try:
        shutil.rmtree(attempt)
    except OSError as exc:
        raise io_error(
            "EXPORT_STAGING_CLEANUP_FAILED", "export staging could not be removed"
        ) from exc


def cleanup_stale_attempts(workspace: JobWorkspace) -> None:
    root = workspace.delivery_staging_root
    try:
        attempts = list(root.iterdir())
    except OSError as exc:
        raise io_error("EXPORT_STAGING_INVALID", "export staging could not be listed") from exc
    if len(attempts) > _MAXIMUM_STALE_ATTEMPTS:
        raise integrity_error("EXPORT_STAGING_INVALID", "too many stale export attempts exist")
    for attempt in attempts:
        _remove_delivery_attempt(root, attempt)


def _read_bounded(path: Path, *, maximum: int, code: str) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except FileNotFoundError as exc:
        raise integrity_error(code, "required delivery artifact does not exist") from exc
    except OSError as exc:
        raise io_error(code, "required delivery artifact could not be read") from exc
    if len(payload) > maximum:
        raise integrity_error(code, "required delivery artifact exceeds its size limit")
    return payload


def _read_model[ModelT: BaseModel](
    path: Path,
    model: type[ModelT],
    *,
    code: str,
) -> ModelT:
    payload = _read_bounded(path, maximum=_MAX_MANIFEST_BYTES, code=code)
    try:
        result = model.model_validate(json.loads(payload.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise integrity_error(code, "required delivery artifact is malformed") from exc
    if payload != pretty_json_bytes(result):
        raise integrity_error(code, "required delivery artifact is not canonical")
    return result


def _write_or_verify_model(
    path: Path,
    value: BaseModel,
    model: type[BaseModel],
    *,
    code: str,
) -> None:
    expected = pretty_json_bytes(value)
    if path.exists():
        actual = _read_bounded(path, maximum=_MAX_MANIFEST_BYTES, code=code)
        try:
            parsed = model.model_validate(json.loads(actual.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise integrity_error(code, "existing delivery artifact is malformed") from exc
        if actual != expected or parsed != value:
            raise integrity_error(code, "existing delivery artifact does not match")
        return
    atomic_write_new_bytes(path, expected, private=True)


def _artifact(path: Path, *, artifact_id: str, artifact_type: str, relative: str) -> ArtifactRef:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise io_error(
            "DELIVERY_ARTIFACT_READ_FAILED", "delivery artifact could not be read"
        ) from exc
    if size < 1:
        raise integrity_error("DELIVERY_ARTIFACT_INVALID", "delivery artifact is empty")
    return ArtifactRef(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        content_sha256=sha256_file(path),
        size_bytes=size,
        relative_storage_path=relative,
    )


def _verify_artifact(workspace: JobWorkspace, artifact: ArtifactRef, *, code: str) -> None:
    path = workspace.safe_path(artifact.relative_storage_path)
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise integrity_error(code, "bound delivery artifact does not exist") from exc
    except OSError as exc:
        raise io_error(code, "bound delivery artifact could not be inspected") from exc
    if size != artifact.size_bytes or sha256_file(path) != artifact.content_sha256:
        raise integrity_error(code, "bound delivery artifact does not match its reference")


def _read_verified_bytes(path: Path, artifact: ArtifactRef, *, code: str) -> bytes:
    payload = _read_bounded(path, maximum=_MAX_DRAFT_BYTES, code=code)
    if len(payload) != artifact.size_bytes or sha256_bytes(payload) != artifact.content_sha256:
        raise integrity_error(code, "bound delivery artifact changed during verification")
    return payload


def _verified_draft_bytes(
    workspace: JobWorkspace,
    artifact: ArtifactRef,
    *,
    code: str,
) -> bytes:
    return _read_verified_bytes(
        workspace.safe_path(artifact.relative_storage_path), artifact, code=code
    )


def _contains_forbidden_approval_marker(payload: bytes) -> bool:
    return any(marker in payload for marker in _FORBIDDEN_APPROVAL_MARKER_BYTES)


def _require_marker_free(payload: bytes) -> None:
    if _contains_forbidden_approval_marker(payload):
        raise state_error(
            "APPROVAL_CONFIRMATION_UNRESOLVED",
            "approved draft contains an unresolved confirmation marker",
        )


def ensure_approved_draft_marker_free(
    workspace: JobWorkspace,
    artifact: ArtifactRef,
) -> bytes:
    """Return fully verified approved bytes only when they contain no confirmation marker."""

    payload = _verified_draft_bytes(workspace, artifact, code="APPROVAL_STALE")
    _require_marker_free(payload)
    return payload


def ensure_approval_confirmation_resolved(
    workspace: JobWorkspace,
    draft: DraftResult,
    human_review: HumanReviewResult,
) -> None:
    """Reject approval until verified review spans and draft markers are both resolved."""

    payload = _verified_draft_bytes(
        workspace,
        draft.manifest.draft_artifact,
        code="APPROVAL_STALE",
    )
    if human_review.bundle.confirmation_spans or _contains_forbidden_approval_marker(payload):
        raise state_error(
            "APPROVAL_CONFIRMATION_UNRESOLVED",
            "human review confirmation spans or draft confirmation markers remain unresolved",
        )


def _approval_intent_values(
    job: JobSpec,
    draft: DraftResult,
    audit: AuditResult,
    human_review: HumanReviewResult,
    reviewer: ReviewerIdentity,
    basis: str,
    unresolved_notes: list[str],
) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "job_fingerprint": job_fingerprint(job),
        "human_review_bundle_fingerprint": human_review.manifest.bundle_fingerprint,
        "human_review_artifact": human_review.manifest.bundle_artifact.model_dump(mode="json"),
        "current_audit_bundle_fingerprint": audit.manifest.bundle_fingerprint,
        "current_audit_findings_artifact": audit.manifest.findings_artifact.model_dump(mode="json"),
        "approved_draft_version": draft.manifest.draft_version,
        "approved_draft_artifact": draft.manifest.draft_artifact.model_dump(mode="json"),
        "approval_target_sha256": human_review.manifest.approval_target_sha256,
        "confirmed_by_user": True,
        "reviewer": reviewer.model_dump(mode="json"),
        "basis": basis,
        "unresolved_notes": unresolved_notes,
    }


def _build_approval_record(
    job: JobSpec,
    draft: DraftResult,
    audit: AuditResult,
    human_review: HumanReviewResult,
    *,
    reviewer: ReviewerIdentity,
    basis: str,
    unresolved_notes: list[str],
    approved_at: datetime,
) -> ApprovalRecord:
    intent_values = _approval_intent_values(
        job,
        draft,
        audit,
        human_review,
        reviewer,
        basis,
        unresolved_notes,
    )
    intent = approval_intent_fingerprint(intent_values)
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "sequence": 1,
        "approval_id": f"approval-{intent[:16]}",
        **intent_values,
        "approved_at": approved_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "intent_fingerprint": intent,
        "previous_record_sha256": None,
    }
    payload["record_sha256"] = approval_record_fingerprint(payload)
    return ApprovalRecord.model_validate(payload)


def read_approval_records(workspace: JobWorkspace) -> tuple[ApprovalRecord, ...]:
    payload = _read_bounded(
        workspace.approval_records_jsonl,
        maximum=_MAX_APPROVAL_LEDGER_BYTES,
        code="APPROVAL_LEDGER_INVALID",
    )
    if not payload or not payload.endswith(b"\n"):
        raise integrity_error(
            "APPROVAL_LEDGER_INVALID",
            "approval ledger is empty or has an incomplete record",
        )
    records: list[ApprovalRecord] = []
    try:
        for line in payload.splitlines():
            record = ApprovalRecord.model_validate(json.loads(line.decode("utf-8")))
            if line != canonical_json_bytes(record):
                raise ValueError("approval record is not canonical JSON")
            records.append(record)
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise integrity_error("APPROVAL_LEDGER_INVALID", "approval ledger is malformed") from exc
    if len(records) != 1:
        raise integrity_error(
            "APPROVAL_LEDGER_INVALID",
            "approval v1 requires exactly one append-only record",
        )
    record = records[0]
    if record.sequence != 1 or record.previous_record_sha256 is not None:
        raise integrity_error("APPROVAL_LEDGER_INVALID", "approval chain is invalid")
    return (record,)


def _approval_record_artifact(workspace: JobWorkspace) -> ArtifactRef:
    return _artifact(
        workspace.approval_records_jsonl,
        artifact_id="approval-records",
        artifact_type="approval_records_jsonl",
        relative="approval/records.jsonl",
    )


def _validate_record_upstream(
    record: ApprovalRecord,
    job: JobSpec,
    draft: DraftResult,
    audit: AuditResult,
    human_review: HumanReviewResult,
) -> None:
    expected = _approval_intent_values(
        job,
        draft,
        audit,
        human_review,
        record.reviewer,
        record.basis,
        list(record.unresolved_notes),
    )
    if (
        record.job_id != job.job_id
        or record.job_fingerprint != job_fingerprint(job)
        or record.intent_fingerprint != approval_intent_fingerprint(expected)
        or record.human_review_artifact != human_review.manifest.bundle_artifact
        or record.current_audit_findings_artifact != audit.manifest.findings_artifact
        or record.approved_draft_artifact != draft.manifest.draft_artifact
        or record.approval_target_sha256 != human_review.manifest.approval_target_sha256
    ):
        raise integrity_error(
            "APPROVAL_STALE", "approval no longer matches verified upstream state"
        )


def _build_approval_manifests(
    job: JobSpec,
    record: ApprovalRecord,
    ledger_artifact: ArtifactRef,
) -> tuple[ApprovalManifest, StageManifest]:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "job_id": job.job_id,
        "job_fingerprint": job_fingerprint(job),
        "approval_id": record.approval_id,
        "approval_record_sequence": record.sequence,
        "approval_record_sha256": record.record_sha256,
        "approval_record_artifact": ledger_artifact.model_dump(mode="json"),
        "approval_intent_fingerprint": record.intent_fingerprint,
        "human_review_bundle_fingerprint": record.human_review_bundle_fingerprint,
        "human_review_artifact": record.human_review_artifact.model_dump(mode="json"),
        "current_audit_bundle_fingerprint": record.current_audit_bundle_fingerprint,
        "current_audit_findings_artifact": (
            record.current_audit_findings_artifact.model_dump(mode="json")
        ),
        "approved_draft_version": record.approved_draft_version,
        "approved_draft_artifact": record.approved_draft_artifact.model_dump(mode="json"),
        "approval_target_sha256": record.approval_target_sha256,
        "created_at": record.approved_at.isoformat().replace("+00:00", "Z"),
    }
    values["manifest_fingerprint"] = approval_manifest_fingerprint(values)
    manifest = ApprovalManifest.model_validate(values)
    stage = StageManifest(
        job_id=job.job_id,
        stage="approval",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.HUMAN_APPROVED,
        input_artifacts=[
            record.approved_draft_artifact,
            record.human_review_artifact,
            record.current_audit_findings_artifact,
        ],
        output_artifacts=[ledger_artifact],
        producer=_APPROVAL_PRODUCER,
        configuration_sha256=manifest.manifest_fingerprint,
        started_at=record.approved_at,
        finished_at=record.approved_at,
    )
    return manifest, stage


def _load_approval_manifests(
    workspace: JobWorkspace,
    job: JobSpec,
    record: ApprovalRecord,
    ledger_artifact: ArtifactRef,
) -> ApprovalResult:
    expected_manifest, expected_stage = _build_approval_manifests(job, record, ledger_artifact)
    manifest = _read_model(
        workspace.approval_manifest_json,
        ApprovalManifest,
        code="APPROVAL_MANIFEST_INVALID",
    )
    stage = _read_model(
        workspace.approval_stage_manifest_json,
        StageManifest,
        code="APPROVAL_STAGE_INVALID",
    )
    if manifest != expected_manifest or stage != expected_stage:
        raise integrity_error("APPROVAL_MANIFEST_INVALID", "approval manifests are inconsistent")
    return ApprovalResult(record=record, manifest=manifest, stage_manifest=stage, reused=True)


def load_approval(
    workspace: JobWorkspace,
    job: JobSpec,
    draft: DraftResult,
    audit: AuditResult,
    human_review: HumanReviewResult,
) -> ApprovalResult:
    record = read_approval_records(workspace)[0]
    _validate_record_upstream(record, job, draft, audit, human_review)
    try:
        ensure_approval_confirmation_resolved(workspace, draft, human_review)
    except VepError as exc:
        if exc.code != "APPROVAL_CONFIRMATION_UNRESOLVED":
            raise
        raise integrity_error(
            "APPROVAL_STALE",
            "approval contains unresolved confirmation content",
        ) from exc
    ledger_artifact = _approval_record_artifact(workspace)
    _verify_artifact(workspace, ledger_artifact, code="APPROVAL_LEDGER_INVALID")
    return _load_approval_manifests(workspace, job, record, ledger_artifact)


def approve_or_recover(
    workspace: JobWorkspace,
    job: JobSpec,
    draft: DraftResult,
    audit: AuditResult,
    human_review: HumanReviewResult,
    *,
    reviewer: ReviewerIdentity,
    basis: str,
    unresolved_notes: list[str],
    approved_at: datetime,
) -> ApprovalResult:
    ensure_approval_confirmation_resolved(workspace, draft, human_review)
    existed = workspace.approval_records_jsonl.exists()
    requested_intent = approval_intent_fingerprint(
        _approval_intent_values(
            job,
            draft,
            audit,
            human_review,
            reviewer,
            basis,
            unresolved_notes,
        )
    )
    if existed:
        record = read_approval_records(workspace)[0]
        _validate_record_upstream(record, job, draft, audit, human_review)
        if record.intent_fingerprint != requested_intent:
            raise state_error(
                "APPROVAL_REPLAY_MISMATCH",
                "approval retry does not match the durable approval intent",
            )
    else:
        record = _build_approval_record(
            job,
            draft,
            audit,
            human_review,
            reviewer=reviewer,
            basis=basis,
            unresolved_notes=unresolved_notes,
            approved_at=approved_at,
        )
        append_jsonl(workspace.approval_records_jsonl, record)
    ledger_artifact = _approval_record_artifact(workspace)
    manifest, stage = _build_approval_manifests(job, record, ledger_artifact)
    _write_or_verify_model(
        workspace.approval_stage_manifest_json,
        stage,
        StageManifest,
        code="APPROVAL_STAGE_INVALID",
    )
    _write_or_verify_model(
        workspace.approval_manifest_json,
        manifest,
        ApprovalManifest,
        code="APPROVAL_MANIFEST_INVALID",
    )
    return ApprovalResult(record=record, manifest=manifest, stage_manifest=stage, reused=existed)


def _final_relative(record: ApprovalRecord) -> str:
    return f"exports/markdown/{record.record_sha256}/final.md"


def _package_relative(record: ApprovalRecord) -> str:
    return f"exports/markdown/{record.record_sha256}/package-manifest.json"


def _expected_final_artifact(record: ApprovalRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id="export-final-markdown",
        artifact_type="markdown",
        content_sha256=record.approved_draft_artifact.content_sha256,
        size_bytes=record.approved_draft_artifact.size_bytes,
        relative_storage_path=_final_relative(record),
    )


def _build_package(
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    approval: ApprovalResult,
    *,
    created_at: datetime,
) -> ExportPackageManifest:
    record = approval.record
    values: dict[str, object] = {
        "schema_version": "1.0",
        "job_id": job.job_id,
        "job_fingerprint": job_fingerprint(job),
        "approval_record_sha256": record.record_sha256,
        "approval_record_artifact": approval.manifest.approval_record_artifact.model_dump(
            mode="json"
        ),
        "approved_draft_version": record.approved_draft_version,
        "approved_draft_artifact": record.approved_draft_artifact.model_dump(mode="json"),
        "human_review_bundle_fingerprint": record.human_review_bundle_fingerprint,
        "human_review_artifact": record.human_review_artifact.model_dump(mode="json"),
        "audit_bundle_fingerprint": record.current_audit_bundle_fingerprint,
        "fixed_evidence_fingerprint": fixed.fixed_evidence_fingerprint,
        "final_artifact": _expected_final_artifact(record).model_dump(mode="json"),
        "format": "markdown",
        "adapter": _EXPORT_ADAPTER.model_dump(mode="json"),
        "byte_exact": True,
        "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    values["package_fingerprint"] = export_package_fingerprint(values)
    return ExportPackageManifest.model_validate(values)


def _package_artifact(workspace: JobWorkspace, record: ApprovalRecord) -> ArtifactRef:
    return _artifact(
        workspace.safe_path(_package_relative(record)),
        artifact_id="export-package-manifest",
        artifact_type="export_package_manifest",
        relative=_package_relative(record),
    )


def _build_export_manifests(
    job: JobSpec,
    package: ExportPackageManifest,
    package_artifact: ArtifactRef,
) -> tuple[ExportManifest, StageManifest]:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "job_id": package.job_id,
        "job_fingerprint": package.job_fingerprint,
        "approval_record_sha256": package.approval_record_sha256,
        "approval_record_artifact": package.approval_record_artifact.model_dump(mode="json"),
        "approved_draft_version": package.approved_draft_version,
        "approved_draft_artifact": package.approved_draft_artifact.model_dump(mode="json"),
        "human_review_bundle_fingerprint": package.human_review_bundle_fingerprint,
        "human_review_artifact": package.human_review_artifact.model_dump(mode="json"),
        "audit_bundle_fingerprint": package.audit_bundle_fingerprint,
        "fixed_evidence_fingerprint": package.fixed_evidence_fingerprint,
        "final_artifact": package.final_artifact.model_dump(mode="json"),
        "package_manifest_artifact": package_artifact.model_dump(mode="json"),
        "package_fingerprint": package.package_fingerprint,
        "format": package.format,
        "adapter": package.adapter.model_dump(mode="json"),
        "byte_exact": package.byte_exact,
        "created_at": package.created_at.isoformat().replace("+00:00", "Z"),
    }
    values["export_fingerprint"] = export_manifest_fingerprint(values)
    manifest = ExportManifest.model_validate(values)
    stage = StageManifest(
        job_id=job.job_id,
        stage="export",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.EXPORTED,
        input_artifacts=[
            package.approval_record_artifact,
            package.approved_draft_artifact,
            package.human_review_artifact,
        ],
        output_artifacts=[package.final_artifact, package_artifact],
        producer=_EXPORT_PRODUCER,
        configuration_sha256=manifest.export_fingerprint,
        started_at=package.created_at,
        finished_at=package.created_at,
    )
    return manifest, stage


def _verify_final(workspace: JobWorkspace, package: ExportPackageManifest) -> None:
    _verify_artifact(workspace, package.final_artifact, code="EXPORT_BUNDLE_INVALID")
    _require_marker_free(
        _read_verified_bytes(
            workspace.safe_path(package.final_artifact.relative_storage_path),
            package.final_artifact,
            code="EXPORT_BUNDLE_INVALID",
        )
    )
    if (
        package.final_artifact.content_sha256 != package.approved_draft_artifact.content_sha256
        or package.final_artifact.size_bytes != package.approved_draft_artifact.size_bytes
    ):
        raise integrity_error("EXPORT_BUNDLE_INVALID", "export is not byte-exact")


def _publish_exact(source: Path, target: Path, expected: ArtifactRef) -> None:
    def verify(path: Path) -> None:
        if (
            path.stat().st_size != expected.size_bytes
            or sha256_file(path) != expected.content_sha256
        ):
            raise integrity_error("EXPORT_BUNDLE_INVALID", "staged export bytes are inconsistent")

    if target.exists():
        verify(target)
        return
    atomic_copy_exclusive(source, target, verifier=verify)
    verify(target)


def load_export(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    approval: ApprovalResult,
) -> ExportResult:
    ensure_approved_draft_marker_free(workspace, approval.record.approved_draft_artifact)
    package = _read_model(
        workspace.safe_path(_package_relative(approval.record)),
        ExportPackageManifest,
        code="EXPORT_BUNDLE_INVALID",
    )
    expected_package = _build_package(
        job,
        fixed,
        approval,
        created_at=package.created_at,
    )
    if package != expected_package:
        raise integrity_error("EXPORT_BUNDLE_INVALID", "export package binding is inconsistent")
    _verify_final(workspace, package)
    package_artifact = _package_artifact(workspace, approval.record)
    expected_manifest, expected_stage = _build_export_manifests(job, package, package_artifact)
    manifest = _read_model(
        workspace.export_manifest_json,
        ExportManifest,
        code="EXPORT_MANIFEST_INVALID",
    )
    stage = _read_model(
        workspace.export_stage_manifest_json,
        StageManifest,
        code="EXPORT_STAGE_INVALID",
    )
    if manifest != expected_manifest or stage != expected_stage:
        raise integrity_error("EXPORT_MANIFEST_INVALID", "export manifests are inconsistent")
    return ExportResult(
        manifest=manifest,
        package_manifest=package,
        stage_manifest=stage,
        reused=True,
    )


def export_or_recover(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    approval: ApprovalResult,
    *,
    created_at: datetime,
) -> ExportResult:
    ensure_approved_draft_marker_free(workspace, approval.record.approved_draft_artifact)
    cleanup_stale_attempts(workspace)
    package_path = workspace.safe_path(_package_relative(approval.record))
    final_path = workspace.safe_path(_final_relative(approval.record))
    had_durable_artifact = final_path.exists() or package_path.exists()
    if package_path.exists():
        if not final_path.exists():
            raise integrity_error(
                "EXPORT_BUNDLE_INVALID",
                "export package exists without its earlier final artifact",
            )
        stored_marker = _read_model(
            package_path,
            ExportPackageManifest,
            code="EXPORT_BUNDLE_INVALID",
        )
        package = _build_package(
            job,
            fixed,
            approval,
            created_at=stored_marker.created_at,
        )
        if stored_marker != package:
            raise integrity_error("EXPORT_BUNDLE_INVALID", "durable export package is inconsistent")
    else:
        package = _build_package(job, fixed, approval, created_at=created_at)
    approved_path = workspace.safe_path(
        approval.record.approved_draft_artifact.relative_storage_path
    )
    attempt = workspace.delivery_staging_root / uuid.uuid4().hex
    ensure_private_directory(attempt)
    try:
        staged_final = attempt / "final.md"
        staged_package = attempt / "package-manifest.json"
        _MARKDOWN_ADAPTER.copy_exact(
            approved_path,
            staged_final,
            approval.record.approved_draft_artifact,
        )
        _require_marker_free(
            _read_verified_bytes(
                staged_final,
                package.final_artifact,
                code="EXPORT_BUNDLE_INVALID",
            )
        )
        atomic_write_new_bytes(staged_package, pretty_json_bytes(package), private=True)
        _MARKDOWN_ADAPTER.copy_exact(staged_final, final_path, package.final_artifact)
        _publish_exact(
            staged_package,
            package_path,
            ArtifactRef(
                artifact_id="export-package-manifest",
                artifact_type="export_package_manifest",
                content_sha256=sha256_bytes(pretty_json_bytes(package)),
                size_bytes=len(pretty_json_bytes(package)),
                relative_storage_path=_package_relative(approval.record),
            ),
        )
    finally:
        if attempt.exists():
            _remove_delivery_attempt(workspace.delivery_staging_root, attempt)

    stored_package = _read_model(
        package_path,
        ExportPackageManifest,
        code="EXPORT_BUNDLE_INVALID",
    )
    if stored_package != package:
        raise integrity_error("EXPORT_BUNDLE_INVALID", "published export package is inconsistent")
    _verify_final(workspace, package)
    package_artifact = _package_artifact(workspace, approval.record)
    manifest, stage = _build_export_manifests(job, package, package_artifact)
    _write_or_verify_model(
        workspace.export_stage_manifest_json,
        stage,
        StageManifest,
        code="EXPORT_STAGE_INVALID",
    )
    _write_or_verify_model(
        workspace.export_manifest_json,
        manifest,
        ExportManifest,
        code="EXPORT_MANIFEST_INVALID",
    )
    return ExportResult(
        manifest=manifest,
        package_manifest=package,
        stage_manifest=stage,
        reused=had_durable_artifact,
    )
