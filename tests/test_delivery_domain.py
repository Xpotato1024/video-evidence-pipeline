from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.delivery import (
    ApprovalRecord,
    ExportAdapterIdentity,
    ExportManifest,
    ExportPackageManifest,
    ReviewerIdentity,
    approval_intent_fingerprint,
    approval_record_fingerprint,
    export_manifest_fingerprint,
    export_package_fingerprint,
)


def _artifact(name: str, digest: str, size: int = 7) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        artifact_type=name,
        content_sha256=digest,
        size_bytes=size,
        relative_storage_path=f"private/{name}.json",
    )


def _approval_record() -> ApprovalRecord:
    draft = _artifact("draft", "1" * 64)
    values: dict[str, object] = {
        "job_id": "delivery-domain",
        "job_fingerprint": "2" * 64,
        "human_review_bundle_fingerprint": "3" * 64,
        "human_review_artifact": _artifact("human-review", "4" * 64).model_dump(mode="json"),
        "current_audit_bundle_fingerprint": "5" * 64,
        "current_audit_findings_artifact": _artifact("audit", "6" * 64).model_dump(mode="json"),
        "approved_draft_version": "v001",
        "approved_draft_artifact": draft.model_dump(mode="json"),
        "approval_target_sha256": draft.content_sha256,
        "confirmed_by_user": True,
        "reviewer": ReviewerIdentity(identity="reviewer-1").model_dump(mode="json"),
        "basis": "Reviewed the exact bounded bundle.",
        "unresolved_notes": ["One non-blocking uncertainty remains."],
    }
    intent = approval_intent_fingerprint(values)
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "sequence": 1,
        "approval_id": f"approval-{intent[:16]}",
        **values,
        "approved_at": datetime(2026, 8, 10, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        "intent_fingerprint": intent,
        "previous_record_sha256": None,
    }
    payload["record_sha256"] = approval_record_fingerprint(payload)
    return ApprovalRecord.model_validate(payload)


def test_approval_record_binds_confirmation_asserted_identity_and_self_hash() -> None:
    record = _approval_record()
    assert record.confirmed_by_user is True
    assert record.reviewer.assertion_method == "cli_asserted"
    assert record.reviewer.cryptographically_verified is False

    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate({**record.model_dump(mode="json"), "record_sha256": "f" * 64})
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate(
            {**record.model_dump(mode="json"), "confirmed_by_user": False}
        )
    with pytest.raises(ValidationError):
        ReviewerIdentity(identity="   ")


def test_export_contracts_require_byte_exact_hash_and_size_binding() -> None:
    record = _approval_record()
    final = record.approved_draft_artifact.model_copy(
        update={
            "artifact_id": "export-final",
            "artifact_type": "markdown",
            "relative_storage_path": "exports/markdown/a/final.md",
        }
    )
    package_values: dict[str, object] = {
        "schema_version": "1.0",
        "job_id": record.job_id,
        "job_fingerprint": record.job_fingerprint,
        "approval_record_sha256": record.record_sha256,
        "approval_record_artifact": _artifact("records", "7" * 64).model_dump(mode="json"),
        "approved_draft_version": record.approved_draft_version,
        "approved_draft_artifact": record.approved_draft_artifact.model_dump(mode="json"),
        "human_review_bundle_fingerprint": record.human_review_bundle_fingerprint,
        "human_review_artifact": record.human_review_artifact.model_dump(mode="json"),
        "audit_bundle_fingerprint": record.current_audit_bundle_fingerprint,
        "fixed_evidence_fingerprint": "8" * 64,
        "final_artifact": final.model_dump(mode="json"),
        "format": "markdown",
        "adapter": ExportAdapterIdentity(version="1.0").model_dump(mode="json"),
        "byte_exact": True,
        "created_at": "2026-08-10T00:01:00Z",
    }
    package_values["package_fingerprint"] = export_package_fingerprint(package_values)
    package = ExportPackageManifest.model_validate(package_values)
    package_artifact = _artifact("package", "9" * 64)
    export_values: dict[str, object] = {
        **{
            key: value
            for key, value in package.model_dump(mode="json").items()
            if key != "package_fingerprint"
        },
        "package_manifest_artifact": package_artifact.model_dump(mode="json"),
        "package_fingerprint": package.package_fingerprint,
    }
    export_values["export_fingerprint"] = export_manifest_fingerprint(export_values)
    manifest = ExportManifest.model_validate(export_values)
    assert manifest.byte_exact is True

    wrong_size = final.model_copy(update={"size_bytes": final.size_bytes + 1})
    invalid = {**export_values, "final_artifact": wrong_size.model_dump(mode="json")}
    invalid["export_fingerprint"] = export_manifest_fingerprint(invalid)
    with pytest.raises(ValidationError):
        ExportManifest.model_validate(invalid)
