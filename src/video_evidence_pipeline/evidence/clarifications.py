"""Record and verify clarification history without rewriting the evidence ledger."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from video_evidence_pipeline.domain.authoring import FixedEvidenceBundle
from video_evidence_pipeline.domain.clarification import ClarificationInput, ClarificationRecord
from video_evidence_pipeline.domain.evidence import AmbiguityRecord, AmbiguityStatus, SourceRefKind
from video_evidence_pipeline.domain.serialization import fingerprint, pretty_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.errors import integrity_error, state_error
from video_evidence_pipeline.evidence.pipeline import _read_bounded_model
from video_evidence_pipeline.storage.atomic import atomic_write_new_bytes
from video_evidence_pipeline.storage.jsonl import exclusive_append_lock
from video_evidence_pipeline.storage.layout import JobWorkspace

_RECORD_NAME = re.compile(r"([1-9][0-9]{0,3})-([0-9a-f]{64})\.json")
_MAX_RECORDS = 1000
_MAX_RECORD_BYTES = 128 * 1024
_EVIDENCE_STATES = frozenset(
    {
        JobState.EVIDENCE_READY,
        JobState.DRAFTED,
        JobState.AUDITED,
        JobState.REVISION_REQUIRED,
        JobState.HUMAN_REVIEW_READY,
        JobState.HUMAN_APPROVED,
        JobState.EXPORTED,
    }
)


def _verified_evidence(path: Path) -> FixedEvidenceBundle:
    # Application owns authorization and stage verification. Import at the call boundary so
    # preparation can use this module without a dependency cycle.
    from video_evidence_pipeline.application import (
        _authorization,
        _load_context,
        _load_fixed_evidence,
        _require_current_authorization,
        _require_local_processing,
    )

    workspace, job, transitions, state = _load_context(path)
    if state not in _EVIDENCE_STATES:
        raise state_error("CLARIFICATION_EVIDENCE_REQUIRED", "the job needs verified evidence")
    auth = _authorization(workspace)
    _require_current_authorization(job, transitions, auth)
    _require_local_processing(auth)
    return _load_fixed_evidence(workspace, job, deep=True)


def _check_input(
    item: ClarificationInput,
    fixed: FixedEvidenceBundle,
    ambiguities: dict[str, AmbiguityRecord],
    active: dict[str, str],
) -> None:
    if item.evidence_bundle_fingerprint != fixed.fixed_evidence_fingerprint:
        raise state_error("CLARIFICATION_EVIDENCE_STALE", "clarification targets other evidence")
    ambiguity = ambiguities.get(item.ambiguity_id)
    if ambiguity is None or ambiguity.status is not AmbiguityStatus.UNRESOLVED:
        raise state_error("CLARIFICATION_TARGET_INVALID", "clarification needs an open ledger item")
    request = fixed.evidence_request
    allowed = {
        (ref.kind, ref.ref_id): ref
        for ref in [*request.transcript_refs, *request.frame_refs, *request.approved_artifact_refs]
    }
    if any(allowed.get((ref.kind, ref.ref_id)) != ref for ref in item.source_refs):
        raise integrity_error(
            "CLARIFICATION_SOURCE_INVALID", "clarification contains an unknown or changed reference"
        )
    if item.decision == "resolved":
        if item.confirmation is None and all(
            ref.kind is SourceRefKind.NOTES for ref in item.source_refs
        ):
            raise state_error(
                "CLARIFICATION_CONFIRMATION_REQUIRED",
                "informal notes require corroboration or an attributed confirmation",
            )
        if item.supersedes != active.get(item.ambiguity_id):
            raise state_error(
                "CLARIFICATION_RESOLUTION_CONFLICT",
                "name the current resolution fingerprint to replace it",
            )


def load_clarifications(
    workspace: JobWorkspace,
    fixed: FixedEvidenceBundle,
    *,
    prefix_count: int | None = None,
) -> list[tuple[str, ClarificationRecord]]:
    root = workspace.safe_path("evidence/clarifications")
    if not root.exists():
        if prefix_count:
            raise integrity_error("CLARIFICATION_LEDGER_INVALID", "clarification prefix is missing")
        return []
    if not root.is_dir():
        raise integrity_error("CLARIFICATION_LEDGER_INVALID", "clarification journal is invalid")
    targets = list(root.iterdir())
    if len(targets) > _MAX_RECORDS:
        raise integrity_error("CLARIFICATION_LEDGER_INVALID", "clarification journal is too large")
    ordered: list[tuple[int, str, Path]] = []
    for target in targets:
        if target.name.startswith(".vep-") and target.name.endswith(".tmp"):
            continue
        match = _RECORD_NAME.fullmatch(target.name)
        if match is None:
            if prefix_count is not None:
                continue
            raise integrity_error("CLARIFICATION_LEDGER_INVALID", "invalid clarification name")
        index = int(match[1])
        if prefix_count is None or index <= prefix_count:
            ordered.append((index, match[2], target))
    ambiguities = {item.ambiguity_id: item for item in fixed.ambiguity_records}
    active: dict[str, str] = {}
    records: list[tuple[str, ClarificationRecord]] = []
    for expected, (index, digest, target) in enumerate(sorted(ordered), start=1):
        value = _read_bounded_model(
            target,
            ClarificationRecord,
            maximum_bytes=_MAX_RECORD_BYTES,
            code="CLARIFICATION_LEDGER_INVALID",
        )
        if index != expected or value.job_id != fixed.job_id or fingerprint(value) != digest:
            raise integrity_error("CLARIFICATION_LEDGER_INVALID", "clarification journal changed")
        _check_input(value.clarification, fixed, ambiguities, active)
        if value.clarification.decision == "resolved":
            active[value.clarification.ambiguity_id] = digest
        records.append((digest, value))
    if prefix_count is not None and len(records) != prefix_count:
        raise integrity_error("CLARIFICATION_LEDGER_INVALID", "clarification prefix is missing")
    return records


def active_resolutions(records: list[tuple[str, ClarificationRecord]]) -> dict[str, str]:
    return {
        record.clarification.ambiguity_id: digest
        for digest, record in records
        if record.clarification.decision == "resolved"
    }


def list_uncertainties(path: Path) -> dict:
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        fixed = _verified_evidence(path)
        records = load_clarifications(workspace, fixed)
    active = active_resolutions(records)
    return {
        "job_id": fixed.job_id,
        "evidence_bundle_fingerprint": fixed.fixed_evidence_fingerprint,
        "items": [
            {
                **item.model_dump(mode="json"),
                "effective_status": "resolved"
                if item.ambiguity_id in active
                else item.status.value,
                "active_resolution": active.get(item.ambiguity_id),
            }
            for item in fixed.ambiguity_records
        ],
        "clarifications": [
            {"fingerprint": digest, **record.model_dump(mode="json")} for digest, record in records
        ],
    }


def record_clarification(
    path: Path,
    response: Path,
    *,
    operator: str,
    confirm: bool,
) -> dict:
    if not confirm:
        raise state_error(
            "CLARIFICATION_CONFIRM_REQUIRED",
            "recording clarification requires explicit confirmation",
        )
    value = _read_bounded_model(
        response.absolute(),
        ClarificationInput,
        maximum_bytes=_MAX_RECORD_BYTES,
        code="CLARIFICATION_INPUT_INVALID",
    )
    workspace = JobWorkspace.open(path)
    with exclusive_append_lock(workspace.transitions_jsonl):
        fixed = _verified_evidence(path)
        try:
            record = ClarificationRecord(
                job_id=fixed.job_id,
                recorded_by=operator,
                recorded_at=datetime.now(UTC),
                clarification=value,
            )
        except ValidationError:
            raise state_error(
                "CLARIFICATION_OPERATOR_INVALID", "name the recording operator"
            ) from None
        records = load_clarifications(workspace, fixed)
        for digest, stored in records:
            if stored.recorded_by == record.recorded_by and stored.clarification == value:
                return {"fingerprint": digest, "reused": True}
        if len(records) >= _MAX_RECORDS:
            raise state_error("CLARIFICATION_LIMIT", "the job reached its clarification log limit")
        _check_input(
            value,
            fixed,
            {item.ambiguity_id: item for item in fixed.ambiguity_records},
            active_resolutions(records),
        )
        digest = fingerprint(record)
        target = workspace.safe_path(f"evidence/clarifications/{len(records) + 1}-{digest}.json")
        payload = pretty_json_bytes(record)
        if len(payload) > _MAX_RECORD_BYTES:
            raise state_error("CLARIFICATION_LIMIT", "the clarification record is too large")
        atomic_write_new_bytes(target, payload, private=True)
    return {"fingerprint": digest, "reused": False}
