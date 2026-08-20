"""Deterministic persistence and validation for the semantic authoring stages."""

from __future__ import annotations

import json
import re
import shutil
import stat
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel, ValidationError

from video_evidence_pipeline import __version__
from video_evidence_pipeline.domain.artifacts import (
    ArtifactRef,
    ProducerIdentity,
    ProducerKind,
    artifact_file_matches,
)
from video_evidence_pipeline.domain.authoring import (
    AgentExecutionMode,
    AgentIdentity,
    AgentRole,
    AgentRunRecord,
    AuditClaimRecord,
    AuditDisposition,
    AuditFinding,
    AuditManifest,
    AuditRequestManifest,
    AuditResponseManifest,
    AuditSeverity,
    AuthoringRequestManifest,
    AuthoringResponseManifest,
    ClaimComparisonRecord,
    ClaimComparisonStatus,
    ClaimRecord,
    ClaimType,
    DraftManifest,
    DraftSpan,
    FixedEvidenceBundle,
    FormatCheck,
    HumanReviewBundle,
    HumanReviewManifest,
    OutputConstraints,
    RequirementCoverage,
    RequirementMap,
    ResolutionDecision,
    ResolutionRecord,
    RevisionHistoryEntry,
    RevisionManifest,
    RevisionRequestManifest,
    RevisionResponseManifest,
    SkillSnapshot,
    fixed_evidence_identity,
    fixed_evidence_identity_matches,
)
from video_evidence_pipeline.domain.evidence import (
    AmbiguityRecord,
    AmbiguityStatus,
    EvidenceManifest,
    EvidenceRecord,
    EvidenceRequestManifest,
    InterpretationLevel,
    SourceRef,
)
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.job import JobSpec, utc_now
from video_evidence_pipeline.domain.length import (
    CharacterLengthAdjustmentAttempt,
    CharacterLengthAdjustmentRequest,
    CharacterLengthCheck,
    LengthBlockManifest,
    resolve_length_constraint,
    validate_character_range,
)
from video_evidence_pipeline.domain.manifests import StageManifest, StageStatus
from video_evidence_pipeline.domain.serialization import (
    canonical_json_bytes,
    fingerprint,
    pretty_json_bytes,
)
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.errors import integrity_error, state_error
from video_evidence_pipeline.storage.atomic import (
    atomic_copy_exclusive,
    atomic_write_bytes,
)
from video_evidence_pipeline.storage.jsonl import append_jsonl
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    enforce_private_file,
    ensure_private_directory,
)

AUTHORING_IMPLEMENTATION_VERSION = "authoring-v1"
_ATTEMPT_NAME = re.compile(r"^[0-9a-f]{32}$")
_MAXIMUM_MACHINE_BYTES = 128 * 1024 * 1024
_MAXIMUM_DRAFT_BYTES = 4 * 1024 * 1024
_MAXIMUM_SKILL_BYTES = 1024 * 1024
_FIXTURE_MARKERS = frozenset({"deterministic", "fake", "fixture", "mock", "synthetic", "test"})
_AUTHOR_SKILLS = frozenset({"draft-video-report", "draft-video-summary"})
_AUDIT_SKILLS = frozenset({"independent-content-audit"})
_REVISION_SKILLS = frozenset({"revise-from-audit"})


@dataclass(frozen=True, slots=True)
class AuthoringPrepareResult:
    request: AuthoringRequestManifest
    reused: bool


@dataclass(frozen=True, slots=True)
class DraftResult:
    manifest: DraftManifest
    stage_manifest: StageManifest
    response: AuthoringResponseManifest | RevisionResponseManifest
    claims: tuple[ClaimRecord, ...]
    requirement_map: RequirementMap
    agent_run: AgentRunRecord
    reused: bool


@dataclass(frozen=True, slots=True)
class AuditPrepareResult:
    request: AuditRequestManifest
    reused: bool


@dataclass(frozen=True, slots=True)
class AuditResult:
    manifest: AuditManifest
    stage_manifest: StageManifest
    response: AuditResponseManifest
    audit_claims: tuple[AuditClaimRecord, ...]
    claim_comparisons: tuple[ClaimComparisonRecord, ...]
    findings: tuple[AuditFinding, ...]
    agent_run: AgentRunRecord
    human_review: HumanReviewManifest | None
    reused: bool


@dataclass(frozen=True, slots=True)
class RevisionPrepareResult:
    request: RevisionRequestManifest
    reused: bool


@dataclass(frozen=True, slots=True)
class RevisionResult:
    draft: DraftResult
    manifest: RevisionManifest
    resolutions: tuple[ResolutionRecord, ...]
    reused: bool


@dataclass(frozen=True, slots=True)
class HumanReviewResult:
    manifest: HumanReviewManifest
    bundle: HumanReviewBundle
    stage_manifest: StageManifest
    reused: bool


AuthoringHistory = tuple[tuple[DraftResult, AuditResult | None], ...]


def _fixed_evidence_fingerprint_matches(
    fixed: FixedEvidenceBundle,
    stored_fingerprint: str,
    *,
    deep: bool = False,
) -> bool:
    return not deep or fixed_evidence_identity_matches(
        job_id=fixed.job_id,
        evidence_request=fixed.evidence_request,
        evidence_manifest=fixed.evidence_manifest,
        evidence_records=fixed.evidence_records,
        ambiguity_records=fixed.ambiguity_records,
        stored_fingerprint=stored_fingerprint,
    )


def _fixed_evidence_bundle_matches(
    stored: FixedEvidenceBundle,
    fixed: FixedEvidenceBundle,
    *,
    deep: bool = False,
) -> bool:
    if not deep:
        stored_values = stored.model_dump(mode="json")
        fixed_values = fixed.model_dump(mode="json")
        for values in (stored_values, fixed_values):
            values.pop("fixed_evidence_fingerprint", None)
            for key in (
                "request_fingerprint",
                "transcript_bundle_fingerprint",
                "media_bundle_fingerprint",
            ):
                values["evidence_request"].pop(key, None)
            values["evidence_request"]["analyzer_contract"].pop("atomic_proposition_contract", None)
            for key in (
                "evidence_bundle_fingerprint",
                "transcript_bundle_fingerprint",
                "media_bundle_fingerprint",
                "request_fingerprint",
                "response_fingerprint",
            ):
                values["evidence_manifest"].pop(key, None)
        return stored_values == fixed_values
    return (
        stored.job_id == fixed.job_id
        and stored.evidence_request == fixed.evidence_request
        and stored.evidence_manifest == fixed.evidence_manifest
        and stored.evidence_records == fixed.evidence_records
        and stored.ambiguity_records == fixed.ambiguity_records
        and _fixed_evidence_fingerprint_matches(fixed, stored.fixed_evidence_fingerprint, deep=deep)
    )


def _validated_history_contexts(
    history: AuthoringHistory,
    *,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    latest_draft: DraftResult,
    deep: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    _validate_fixed_job(job, fixed, code="DRAFT_HISTORY_INVALID")
    if not history or history[-1][0] != latest_draft:
        raise integrity_error("DRAFT_HISTORY_INVALID", "authoring history target is invalid")
    authoring_contexts: list[str] = []
    auditor_contexts: list[str] = []
    previous: DraftResult | None = None
    for index, (draft, audit) in enumerate(history):
        expected_version = f"v{index + 1:03d}"
        if (
            draft.manifest.job_id != job.job_id
            or not _fixed_evidence_fingerprint_matches(
                fixed, draft.manifest.fixed_evidence_fingerprint, deep=deep
            )
            or draft.manifest.draft_version != expected_version
            or draft.manifest.revision_count != index
        ):
            raise integrity_error("DRAFT_HISTORY_INVALID", "authoring history is not contiguous")
        if previous is not None and (
            draft.manifest.predecessor_draft_sha256
            != previous.manifest.draft_artifact.content_sha256
        ):
            raise integrity_error("DRAFT_HISTORY_INVALID", "draft predecessor lineage is invalid")
        if index < len(history) - 1 and (
            audit is None or audit.manifest.disposition is not AuditDisposition.REVISION_REQUIRED
        ):
            raise integrity_error("DRAFT_HISTORY_INVALID", "revision predecessor audit is invalid")
        if audit is not None:
            if (
                audit.manifest.job_id != job.job_id
                or not _fixed_evidence_fingerprint_matches(
                    fixed, audit.manifest.fixed_evidence_fingerprint, deep=deep
                )
                or audit.manifest.target_draft_version != expected_version
                or audit.manifest.target_draft_sha256
                != draft.manifest.draft_artifact.content_sha256
            ):
                raise integrity_error("DRAFT_HISTORY_INVALID", "audit lineage is invalid")
            auditor_contexts.append(audit.manifest.auditor.context_id)
        authoring_contexts.append(draft.manifest.producer.context_id)
        previous = draft
    all_contexts = [*authoring_contexts, *auditor_contexts]
    if len(all_contexts) != len(set(all_contexts)):
        raise integrity_error("DRAFT_HISTORY_INVALID", "agent contexts are not fresh")
    return tuple(sorted(authoring_contexts)), tuple(sorted(auditor_contexts))


def _staging_root(workspace: JobWorkspace) -> Path:
    return workspace.safe_path("semantic/staging")


def create_attempt_directory(workspace: JobWorkspace) -> Path:
    root = _staging_root(workspace)
    ensure_private_directory(root)
    attempt = root / uuid.uuid4().hex
    ensure_private_directory(attempt)
    return attempt


def _valid_attempt(root: Path, attempt: Path) -> bool:
    return (
        attempt.parent == root
        and _ATTEMPT_NAME.fullmatch(attempt.name) is not None
        and not attempt.is_symlink()
        and not getattr(attempt, "is_junction", lambda: False)()
        and not attempt.is_mount()
        and attempt.is_dir()
    )


def cleanup_stale_attempts(workspace: JobWorkspace) -> None:
    root = _staging_root(workspace)
    ensure_private_directory(root)
    for attempt in root.iterdir():
        if not _valid_attempt(root, attempt):
            raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
        for descendant in attempt.rglob("*"):
            if (
                descendant.is_symlink()
                or getattr(descendant, "is_junction", lambda: False)()
                or descendant.is_mount()
            ):
                raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
        shutil.rmtree(attempt)


def cleanup_attempt(workspace: JobWorkspace, attempt: Path) -> None:
    root = _staging_root(workspace)
    if not _valid_attempt(root, attempt):
        raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
    shutil.rmtree(attempt)


def _read_bounded_bytes(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise integrity_error(code, "machine artifact is invalid")
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError:
        raise integrity_error(code, "machine artifact is unavailable") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if len(payload) != before.st_size or identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise integrity_error(code, "machine artifact changed while reading")
    return payload


def _read_model[ModelT: BaseModel](
    path: Path,
    model: type[ModelT],
    *,
    code: str,
    maximum_bytes: int = _MAXIMUM_MACHINE_BYTES,
) -> ModelT:
    payload = _read_bounded_bytes(path, maximum_bytes=maximum_bytes, code=code)
    try:
        return model.model_validate_json(payload, strict=True)
    except (UnicodeError, ValidationError):
        raise integrity_error(code, "machine artifact is malformed") from None


def _read_json_list[ModelT: BaseModel](
    path: Path,
    model: type[ModelT],
    *,
    code: str,
) -> tuple[ModelT, ...]:
    payload = _read_bounded_bytes(path, maximum_bytes=_MAXIMUM_MACHINE_BYTES, code=code)
    try:
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError
        return tuple(
            model.model_validate_json(canonical_json_bytes(item), strict=True) for item in data
        )
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError):
        raise integrity_error(code, "machine artifact is malformed") from None


def _jsonl_bytes(records: tuple[BaseModel, ...] | list[BaseModel]) -> bytes:
    return b"".join(canonical_json_bytes(item) + b"\n" for item in records)


def _read_jsonl[ModelT: BaseModel](
    path: Path,
    model: type[ModelT],
    *,
    code: str,
) -> tuple[ModelT, ...]:
    payload = _read_bounded_bytes(path, maximum_bytes=_MAXIMUM_MACHINE_BYTES, code=code)
    if not payload.endswith(b"\n"):
        raise integrity_error(code, "JSONL artifact is incomplete")
    try:
        return tuple(model.model_validate_json(line, strict=True) for line in payload.splitlines())
    except ValidationError:
        raise integrity_error(code, "JSONL artifact is malformed") from None


def _verify_artifact(path: Path, artifact: ArtifactRef, *, deep: bool = False) -> None:
    try:
        if not artifact_file_matches(path, artifact, deep=deep, hasher=sha256_file):
            raise integrity_error("AUTHORING_BUNDLE_TAMPERED", "authoring artifact is invalid")
        enforce_private_file(path)
    except OSError:
        raise integrity_error(
            "AUTHORING_BUNDLE_TAMPERED", "authoring artifact is unavailable"
        ) from None


def _artifact(path: Path, *, artifact_type: str, relative_path: str) -> ArtifactRef:
    content_sha256 = sha256_file(path)
    return ArtifactRef(
        artifact_id=f"{artifact_type}-{content_sha256[:16]}",
        artifact_type=artifact_type,
        content_sha256=content_sha256,
        size_bytes=path.stat(follow_symlinks=False).st_size,
        relative_storage_path=relative_path,
    )


def _existing_artifact(
    workspace: JobWorkspace,
    relative_path: str,
    *,
    artifact_type: str,
    code: str,
) -> ArtifactRef:
    """Boundedly load a published orphan artifact before adopting it on retry."""

    path = workspace.safe_path(relative_path)
    _read_bounded_bytes(path, maximum_bytes=_MAXIMUM_MACHINE_BYTES, code=code)
    artifact = _artifact(path, artifact_type=artifact_type, relative_path=relative_path)
    _verify_artifact(path, artifact)
    return artifact


def _artifact_payloads(items: list[ArtifactRef]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in items]


def _publish(workspace: JobWorkspace, source: Path, artifact: ArtifactRef) -> None:
    target = workspace.safe_path(artifact.relative_storage_path)
    ensure_private_directory(target.parent)
    atomic_copy_exclusive(source, target, verifier=lambda item: _verify_artifact(item, artifact))
    _verify_artifact(target, artifact)


def _write_or_verify_model(
    path: Path, value: BaseModel, model: type[BaseModel], *, code: str
) -> bool:
    if path.exists():
        stored = _read_model(path, model, code=code)
        if stored != value:
            raise integrity_error(code, "canonical request or manifest changed")
        return True
    _write_machine_json(path, value, code=code)
    stored = _read_model(path, model, code=code)
    if stored != value:
        raise integrity_error(code, "canonical request or manifest changed")
    return False


def _write_machine_json(path: Path, value: BaseModel, *, code: str) -> None:
    payload = pretty_json_bytes(value)
    if not payload or len(payload) > _MAXIMUM_MACHINE_BYTES:
        raise integrity_error(code, "machine artifact exceeds the canonical byte limit")
    atomic_write_bytes(path, payload, private=True)


def _response_is_fixture(identity: AgentIdentity) -> bool:
    values = (identity.provider, identity.model, identity.version, identity.executor)
    tokens = {token for value in values for token in re.findall(r"[a-z0-9]+", value.casefold())}
    return identity.execution_mode is AgentExecutionMode.TEST_FIXTURE or bool(
        tokens & _FIXTURE_MARKERS
    )


def _validate_execution(identity: AgentIdentity, *, allow_fixture: bool) -> None:
    if _response_is_fixture(identity) and not allow_fixture:
        raise state_error("FIXTURE_AGENT_FORBIDDEN", "fixture agent output is not allowed")


def _same_skill_snapshot_except_lifecycle(
    frozen: SkillSnapshot,
    current: SkillSnapshot,
) -> bool:
    """Compare the immutable execution contract without treating promotion as drift."""

    return frozen.model_dump(mode="json", exclude={"lifecycle"}) == current.model_dump(
        mode="json", exclude={"lifecycle"}
    )


def _validate_import_skill(
    workspace: JobWorkspace,
    skill: SkillSnapshot,
    identity: AgentIdentity,
    *,
    allowed: frozenset[str],
    allow_fixture: bool,
) -> None:
    """Gate only a new production import against the current Skill snapshot.

    Saved requests and completed artifacts deliberately retain their original
    snapshot.  A fixture response is an internal test route and never obtains a
    production capability from this check.
    """

    if allow_fixture and _response_is_fixture(identity):
        return
    current = load_skill_snapshot(workspace, skill.skill_id, allowed=allowed)
    if not _same_skill_snapshot_except_lifecycle(skill, current):
        raise integrity_error(
            "SKILL_SNAPSHOT_STALE",
            "current Skill content, evaluation, or identity differs from the request snapshot",
        )
    if current.lifecycle != "active":
        raise state_error(
            "DRAFT_SKILL_FORBIDDEN",
            "draft Skill output is not allowed on a production import route",
        )


def _repository_root(workspace: JobWorkspace) -> Path:
    return workspace.jobs_root.parent.parent


def load_skill_snapshot(
    workspace: JobWorkspace,
    skill_id: str,
    *,
    allowed: frozenset[str],
) -> SkillSnapshot:
    if skill_id not in allowed:
        raise state_error("SKILL_ROUTE_INVALID", "selected Skill is not valid for this stage")
    root = _repository_root(workspace)
    registry_path = root / ".agents" / "skill-system.toml"
    payload = _read_bounded_bytes(
        registry_path,
        maximum_bytes=_MAXIMUM_SKILL_BYTES,
        code="SKILL_REGISTRY_INVALID",
    )
    try:
        registry = tomllib.loads(payload.decode("utf-8"))
        entries = [item for item in registry["skills"] if item.get("id") == skill_id]
        if len(entries) != 1:
            raise ValueError
        entry = entries[0]
        relative_path = entry["path"].replace("\\", "/")
        eval_relative = entry["eval_path"].replace("\\", "/")
        if (
            entry["automatic_trigger"] is not False
            or entry["side_effect_policy"] != "instruction-only"
            or entry["lifecycle"] not in {"draft", "active"}
        ):
            raise ValueError
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
        raise integrity_error("SKILL_REGISTRY_INVALID", "Skill registry is invalid") from None
    skill_path = root.joinpath(*relative_path.split("/"))
    eval_path = root.joinpath(*eval_relative.split("/"))
    for path in (skill_path, eval_path):
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            raise integrity_error("SKILL_REGISTRY_INVALID", "Skill path is invalid") from None
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise integrity_error("SKILL_REGISTRY_INVALID", "Skill path is invalid")
    skill_bytes = _read_bounded_bytes(
        skill_path, maximum_bytes=_MAXIMUM_SKILL_BYTES, code="SKILL_SNAPSHOT_INVALID"
    )
    eval_bytes = _read_bounded_bytes(
        eval_path, maximum_bytes=_MAXIMUM_SKILL_BYTES, code="SKILL_SNAPSHOT_INVALID"
    )
    return SkillSnapshot(
        skill_id=skill_id,
        relative_path=relative_path,
        content_sha256=sha256_bytes(skill_bytes),
        eval_sha256=sha256_bytes(eval_bytes),
        lifecycle=entry["lifecycle"],
        automatic_trigger=False,
        side_effect_policy="instruction-only",
        explicit_selection=True,
        owner=entry["owner"],
    )


def build_fixed_evidence_bundle(
    job: JobSpec,
    request: EvidenceRequestManifest,
    manifest: EvidenceManifest,
    evidence_records: tuple[EvidenceRecord, ...],
    ambiguity_records: tuple[AmbiguityRecord, ...],
) -> FixedEvidenceBundle:
    evidence = list(evidence_records)
    ambiguities = list(ambiguity_records)
    identity = fixed_evidence_identity(
        job_id=job.job_id,
        evidence_request=request,
        evidence_manifest=manifest,
        evidence_records=evidence,
        ambiguity_records=ambiguities,
    )
    return FixedEvidenceBundle(
        job_id=job.job_id,
        fixed_evidence_fingerprint=identity,
        evidence_request=request,
        evidence_manifest=manifest,
        evidence_records=evidence,
        ambiguity_records=ambiguities,
    )


def _constraints(job: JobSpec, fixed: FixedEvidenceBundle) -> OutputConstraints:
    effective_length = resolve_length_constraint(job.assignment.length)
    return OutputConstraints(
        format=job.output.format,
        language=job.assignment.language,
        task_type=job.assignment.task_type,
        requirements=fixed.evidence_request.assignment.requirements,
        length=job.assignment.length if effective_length is not None else None,
        effective_length=effective_length,
    )


def _length_attempt_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"authoring/length/{version}-attempts.jsonl")


def _length_request_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"authoring/length/{version}-request.json")


def _length_candidate_path(
    workspace: JobWorkspace, version: str, response_fingerprint: str
) -> tuple[Path, str]:
    relative = f"authoring/length/{version}/{response_fingerprint}/candidate-response.json"
    return workspace.safe_path(relative), relative


def _length_attempts(
    workspace: JobWorkspace, version: str
) -> tuple[CharacterLengthAdjustmentAttempt, ...]:
    path = _length_attempt_path(workspace, version)
    if not path.exists():
        return ()
    return _read_jsonl(
        path,
        CharacterLengthAdjustmentAttempt,
        code="LENGTH_ADJUSTMENT_HISTORY_INVALID",
    )


def _durable_length_candidate(
    workspace: JobWorkspace,
    *,
    version: str,
    response_fingerprint: str,
    response_payload: bytes,
) -> ArtifactRef:
    path, relative = _length_candidate_path(workspace, version, response_fingerprint)
    ensure_private_directory(path.parent.parent)
    ensure_private_directory(path.parent)
    if path.exists():
        if (
            _read_bounded_bytes(
                path,
                maximum_bytes=_MAXIMUM_MACHINE_BYTES,
                code="LENGTH_CANDIDATE_INVALID",
            )
            != response_payload
        ):
            raise integrity_error(
                "LENGTH_CANDIDATE_INVALID", "length candidate identity is ambiguous"
            )
    else:
        atomic_write_bytes(path, response_payload, private=True)
    return _artifact(
        path,
        artifact_type="failed_length_candidate_response",
        relative_path=relative,
    )


def _recover_orphan_length_candidate(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    version: str,
    effective,
    request_fingerprint: str,
    attempts: tuple[CharacterLengthAdjustmentAttempt, ...],
) -> tuple[CharacterLengthAdjustmentAttempt, ...]:
    candidate_root = workspace.safe_path(f"authoring/length/{version}")
    if not candidate_root.exists():
        return attempts
    referenced = {item.response_fingerprint for item in attempts}
    orphan_fingerprints = sorted(
        child.name
        for child in candidate_root.iterdir()
        if re.fullmatch(r"[0-9a-f]{64}", child.name)
        and child.name not in referenced
        and (child / "candidate-response.json").is_file()
    )
    if not orphan_fingerprints:
        return attempts
    if len(orphan_fingerprints) != 1:
        raise integrity_error(
            "LENGTH_CANDIDATE_INVALID", "multiple unreferenced length candidates are ambiguous"
        )
    response_fingerprint = orphan_fingerprints[0]
    candidate_path, relative = _length_candidate_path(workspace, version, response_fingerprint)
    response_payload = _read_bounded_bytes(
        candidate_path,
        maximum_bytes=_MAXIMUM_MACHINE_BYTES,
        code="LENGTH_CANDIDATE_INVALID",
    )
    response_type = AuthoringResponseManifest if version == "v001" else RevisionResponseManifest
    try:
        response = response_type.model_validate_json(response_payload)
    except ValidationError:
        raise integrity_error(
            "LENGTH_CANDIDATE_INVALID", "orphan length candidate is invalid"
        ) from None
    if (
        response.request_fingerprint != request_fingerprint
        or pretty_json_bytes(response) != response_payload
        or fingerprint(response) != response_fingerprint
    ):
        raise integrity_error(
            "LENGTH_CANDIDATE_INVALID", "orphan length candidate identity is invalid"
        )
    check = validate_character_range(response.draft_markdown, effective)
    if check is None or check.passed:
        raise integrity_error(
            "LENGTH_CANDIDATE_INVALID", "orphan length candidate is not a failed gate"
        )
    correction_count = sum(item.correction_number > 0 for item in attempts)
    correction_number = 0 if not attempts else correction_count + 1
    if correction_number > check.effective.maximum_adjustments:
        raise integrity_error(
            "LENGTH_ADJUSTMENT_HISTORY_INVALID", "orphan length candidate exceeds the budget"
        )
    attempt = CharacterLengthAdjustmentAttempt(
        job_id=job.job_id,
        target_draft_version=version,
        correction_number=correction_number,
        response_fingerprint=response_fingerprint,
        candidate_artifact=_artifact(
            candidate_path,
            artifact_type="failed_length_candidate_response",
            relative_path=relative,
        ),
        check=check,
        created_at=response.created_at,
    )
    append_jsonl(_length_attempt_path(workspace, version), attempt)
    return (*attempts, attempt)


def _length_request_matches_attempt(
    request: CharacterLengthAdjustmentRequest,
    job: JobSpec,
    version: str,
    attempt: CharacterLengthAdjustmentAttempt,
) -> bool:
    check = attempt.check
    return (
        request.job_id == job.job_id
        and request.target_draft_version == version
        and request.actual_chars == check.actual_chars
        and request.effective_min_chars == check.effective.min_chars
        and request.effective_max_chars == check.effective.max_chars
        and request.below_minimum_by == check.below_minimum_by
        and request.over_maximum_by == check.over_maximum_by
        and request.adjustment_number == attempt.correction_number + 1
        and request.adjustments_remaining_after_this
        == check.effective.maximum_adjustments - request.adjustment_number
        and request.candidate_fingerprint == attempt.response_fingerprint
        and request.candidate_artifact == attempt.candidate_artifact
        and request.created_at == attempt.created_at
    )


def _length_adjustment_request_for_attempt(
    job: JobSpec,
    version: str,
    attempt: CharacterLengthAdjustmentAttempt,
) -> CharacterLengthAdjustmentRequest:
    check = attempt.check
    adjustment_number = attempt.correction_number + 1
    return CharacterLengthAdjustmentRequest(
        job_id=job.job_id,
        target_draft_version=version,
        actual_chars=check.actual_chars,
        effective_min_chars=check.effective.min_chars,
        effective_max_chars=check.effective.max_chars,
        below_minimum_by=check.below_minimum_by,
        over_maximum_by=check.over_maximum_by,
        adjustment_number=adjustment_number,
        adjustments_remaining_after_this=(check.effective.maximum_adjustments - adjustment_number),
        candidate_fingerprint=attempt.response_fingerprint,
        candidate_artifact=attempt.candidate_artifact,
        created_at=attempt.created_at,
    )


def _expected_length_adjustment_request(
    job: JobSpec,
    version: str,
    attempts: tuple[CharacterLengthAdjustmentAttempt, ...],
) -> CharacterLengthAdjustmentRequest:
    maximum = attempts[-1].check.effective.maximum_adjustments
    request_attempt = attempts[-2] if attempts[-1].correction_number == maximum else attempts[-1]
    return _length_adjustment_request_for_attempt(job, version, request_attempt)


def _recover_length_adjustment_request(
    workspace: JobWorkspace,
    job: JobSpec,
    version: str,
    attempts: tuple[CharacterLengthAdjustmentAttempt, ...],
) -> CharacterLengthAdjustmentRequest:
    expected = _expected_length_adjustment_request(job, version, attempts)
    request_path = _length_request_path(workspace, version)
    if not request_path.exists():
        _write_machine_json(
            request_path,
            expected,
            code="LENGTH_ADJUSTMENT_REQUEST_WRITE_FAILED",
        )
        return expected
    request = _read_model(
        request_path,
        CharacterLengthAdjustmentRequest,
        maximum_bytes=_MAXIMUM_MACHINE_BYTES,
        code="LENGTH_ADJUSTMENT_REQUEST_INVALID",
    )
    if request != expected:
        if any(
            _length_request_matches_attempt(request, job, version, prior)
            for prior in attempts
            if prior.correction_number < expected.adjustment_number - 1
        ):
            _write_machine_json(
                request_path,
                expected,
                code="LENGTH_ADJUSTMENT_REQUEST_WRITE_FAILED",
            )
            return expected
        raise integrity_error(
            "LENGTH_ADJUSTMENT_REQUEST_INVALID",
            "length adjustment request does not match the durable ledger",
        )
    return request


def _read_length_adjustment_request(
    workspace: JobWorkspace,
    job: JobSpec,
    version: str,
    attempts: tuple[CharacterLengthAdjustmentAttempt, ...],
) -> CharacterLengthAdjustmentRequest:
    request_path = _length_request_path(workspace, version)
    if not request_path.exists():
        raise integrity_error(
            "LENGTH_ADJUSTMENT_REQUEST_INVALID",
            "length adjustment request is missing",
        )
    request = _read_model(
        request_path,
        CharacterLengthAdjustmentRequest,
        maximum_bytes=_MAXIMUM_MACHINE_BYTES,
        code="LENGTH_ADJUSTMENT_REQUEST_INVALID",
    )
    if request != _expected_length_adjustment_request(job, version, attempts):
        raise integrity_error(
            "LENGTH_ADJUSTMENT_REQUEST_INVALID",
            "length adjustment request does not match the durable ledger",
        )
    return request


def _length_adjustment_message(
    request: CharacterLengthAdjustmentRequest,
    maximum: int,
) -> str:
    required = (
        f"{request.effective_min_chars if request.effective_min_chars is not None else 'none'}-"
        f"{request.effective_max_chars if request.effective_max_chars is not None else 'none'}"
    )
    return (
        f"current character count: {request.actual_chars}; required range: {required}; "
        f"below minimum by: {request.below_minimum_by}; over maximum by: "
        f"{request.over_maximum_by}; adjustment: {request.adjustment_number}/{maximum}"
    )


def _enforce_length_gate(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    version: str,
    draft_markdown: str,
    response_fingerprint: str,
    response_payload: bytes,
    response_created_at: datetime,
    request_fingerprint: str,
) -> tuple[CharacterLengthCheck | None, int]:
    effective = resolve_length_constraint(job.assignment.length)
    check = validate_character_range(draft_markdown, effective)
    attempts = _recover_orphan_length_candidate(
        workspace,
        job,
        version=version,
        effective=effective,
        request_fingerprint=request_fingerprint,
        attempts=_length_attempts(workspace, version),
    )
    if attempts:
        _recover_length_adjustment_request(workspace, job, version, attempts)
    correction_count = sum(item.correction_number > 0 for item in attempts)
    if check is None:
        return check, correction_count
    maximum = check.effective.maximum_adjustments
    if check.passed:
        if attempts and correction_count >= maximum:
            raise state_error(
                "LENGTH_ADJUSTMENT_EXHAUSTED",
                f"length adjustment limit {maximum} reached for {version}",
            )
        return check, correction_count + int(bool(attempts))
    existing = next(
        (item for item in attempts if item.response_fingerprint == response_fingerprint),
        None,
    )
    if existing is not None:
        candidate = _durable_length_candidate(
            workspace,
            version=version,
            response_fingerprint=response_fingerprint,
            response_payload=response_payload,
        )
        if existing.candidate_artifact != candidate or existing.check != check:
            raise integrity_error(
                "LENGTH_ADJUSTMENT_HISTORY_INVALID",
                "duplicate length candidate does not match its durable ledger entry",
            )
        latest = attempts[-1]
        if latest.correction_number >= maximum:
            raise state_error(
                "LENGTH_ADJUSTMENT_EXHAUSTED",
                f"length adjustment limit {maximum} reached for {version}",
            )
        request = _read_length_adjustment_request(workspace, job, version, attempts)
        raise state_error(
            "LENGTH_ADJUSTMENT_REQUIRED",
            _length_adjustment_message(request, maximum),
        )
    correction_number = 0 if not attempts else correction_count + 1
    if correction_number > maximum:
        raise state_error(
            "LENGTH_ADJUSTMENT_EXHAUSTED",
            f"length adjustment limit {maximum} reached for {version}",
        )
    candidate = _durable_length_candidate(
        workspace,
        version=version,
        response_fingerprint=response_fingerprint,
        response_payload=response_payload,
    )
    attempt = CharacterLengthAdjustmentAttempt(
        job_id=job.job_id,
        target_draft_version=version,
        correction_number=correction_number,
        response_fingerprint=response_fingerprint,
        candidate_artifact=candidate,
        check=check,
        created_at=response_created_at,
    )
    append_jsonl(_length_attempt_path(workspace, version), attempt)
    correction_count += int(correction_number > 0)
    if attempt.correction_number >= maximum and attempt.correction_number > 0:
        raise state_error(
            "LENGTH_ADJUSTMENT_EXHAUSTED",
            f"length adjustment limit {maximum} reached for {version}",
        )
    adjustment_request = _length_adjustment_request_for_attempt(job, version, attempt)
    _write_machine_json(
        _length_request_path(workspace, version),
        adjustment_request,
        code="LENGTH_ADJUSTMENT_REQUEST_WRITE_FAILED",
    )
    raise state_error(
        "LENGTH_ADJUSTMENT_REQUIRED",
        _length_adjustment_message(adjustment_request, maximum),
    )


def publish_length_block(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    version: str,
    blocked_stage: str,
) -> LengthBlockManifest:
    attempts = _validate_length_ledger(
        workspace,
        job,
        version=version,
        blocked_stage=blocked_stage,
        exhausted=True,
        deep=False,
    )
    final = attempts[-1]
    maximum = final.check.effective.maximum_adjustments
    if final.correction_number != maximum:
        raise integrity_error("LENGTH_BLOCK_INVALID", "length adjustment budget is not exhausted")
    manifest = LengthBlockManifest(
        job_id=job.job_id,
        blocked_stage=blocked_stage,
        target_draft_version=version,
        effective_min_chars=final.check.effective.min_chars,
        effective_max_chars=final.check.effective.max_chars,
        maximum_adjustments=maximum,
        final_actual_chars=final.check.actual_chars,
        final_below_minimum_by=final.check.below_minimum_by,
        final_over_maximum_by=final.check.over_maximum_by,
        final_candidate_fingerprint=final.response_fingerprint,
        final_candidate_artifact=final.candidate_artifact,
        created_at=final.created_at,
    )
    _write_or_verify_model(
        workspace.safe_path("manifests/authoring/length-block.json"),
        manifest,
        LengthBlockManifest,
        code="LENGTH_BLOCK_INVALID",
    )
    return manifest


def _validate_length_ledger(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    version: str,
    blocked_stage: str,
    exhausted: bool,
    deep: bool,
) -> tuple[CharacterLengthAdjustmentAttempt, ...]:
    attempts = _length_attempts(workspace, version)
    if not attempts:
        raise integrity_error("LENGTH_BLOCK_INVALID", "length adjustment history is absent")
    effective = resolve_length_constraint(job.assignment.length)
    if effective is None:
        raise integrity_error("LENGTH_BLOCK_INVALID", "length constraint is absent")
    maximum = effective.maximum_adjustments
    expected_numbers = list(range(len(attempts)))
    if (
        [item.correction_number for item in attempts] != expected_numbers
        or len({item.response_fingerprint for item in attempts}) != len(attempts)
        or any(
            item.job_id != job.job_id
            or item.target_draft_version != version
            or item.check.effective != effective
            for item in attempts
        )
        or (
            exhausted
            and (len(attempts) != maximum + 1 or attempts[-1].correction_number != maximum)
        )
        or (not exhausted and attempts[-1].correction_number >= maximum)
    ):
        raise integrity_error("LENGTH_BLOCK_INVALID", "length adjustment ledger is not canonical")
    for attempt in attempts:
        candidate_path, expected_relative = _length_candidate_path(
            workspace, version, attempt.response_fingerprint
        )
        if (
            attempt.candidate_artifact.artifact_type != "failed_length_candidate_response"
            or attempt.candidate_artifact.relative_storage_path != expected_relative
        ):
            raise integrity_error(
                "LENGTH_BLOCK_INVALID", "length candidate reference is not canonical"
            )
        _verify_artifact(candidate_path, attempt.candidate_artifact, deep=deep)
        if deep:
            model_type = (
                AuthoringResponseManifest
                if blocked_stage == "initial_authoring"
                else RevisionResponseManifest
            )
            model = _read_model(
                candidate_path,
                model_type,
                maximum_bytes=_MAXIMUM_MACHINE_BYTES,
                code="LENGTH_CANDIDATE_INVALID",
            )
            recalculated = validate_character_range(model.draft_markdown, effective)
            if fingerprint(model) != attempt.response_fingerprint or recalculated != attempt.check:
                raise integrity_error(
                    "LENGTH_CANDIDATE_INVALID",
                    "failed length candidate bytes do not match the durable ledger",
                )
    _read_length_adjustment_request(workspace, job, version, attempts)
    return attempts


def load_length_block(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    deep: bool = False,
    expected_blocked_stage: str | None = None,
) -> LengthBlockManifest:
    manifest = _read_model(
        workspace.safe_path("manifests/authoring/length-block.json"),
        LengthBlockManifest,
        maximum_bytes=_MAXIMUM_MACHINE_BYTES,
        code="LENGTH_BLOCK_INVALID",
    )
    if manifest.job_id != job.job_id or (
        expected_blocked_stage is not None and manifest.blocked_stage != expected_blocked_stage
    ):
        raise integrity_error("LENGTH_BLOCK_INVALID", "length block provenance is invalid")
    attempts = _validate_length_ledger(
        workspace,
        job,
        version=manifest.target_draft_version,
        blocked_stage=manifest.blocked_stage,
        exhausted=True,
        deep=deep,
    )
    final = attempts[-1]
    if (
        final.correction_number != manifest.maximum_adjustments
        or final.check.effective.min_chars != manifest.effective_min_chars
        or final.check.effective.max_chars != manifest.effective_max_chars
        or final.check.effective.maximum_adjustments != manifest.maximum_adjustments
        or final.response_fingerprint != manifest.final_candidate_fingerprint
        or final.candidate_artifact != manifest.final_candidate_artifact
        or final.check.actual_chars != manifest.final_actual_chars
        or final.check.below_minimum_by != manifest.final_below_minimum_by
        or final.check.over_maximum_by != manifest.final_over_maximum_by
    ):
        raise integrity_error("LENGTH_BLOCK_INVALID", "length block ledger binding is invalid")
    return manifest


def _recovered_length_state(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    version: str,
    draft_markdown: str,
) -> tuple[CharacterLengthCheck | None, int]:
    check = validate_character_range(
        draft_markdown,
        resolve_length_constraint(job.assignment.length),
    )
    if check is not None and not check.passed:
        raise integrity_error(
            "DRAFT_BUNDLE_INVALID",
            "recovered draft does not satisfy the effective character range",
        )
    return check, _validate_successful_length_ledger(
        workspace,
        job,
        version=version,
        blocked_stage=("initial_authoring" if version == "v001" else "semantic_revision"),
        deep=False,
    )


def _validate_successful_length_ledger(
    workspace: JobWorkspace,
    job: JobSpec,
    *,
    version: str,
    blocked_stage: str,
    deep: bool,
) -> int:
    attempts = _length_attempts(workspace, version)
    request_path = _length_request_path(workspace, version)
    if not attempts:
        if request_path.exists():
            raise integrity_error(
                "DRAFT_BUNDLE_INVALID",
                "draft length-adjustment request exists without a ledger",
            )
        return 0
    validated = _validate_length_ledger(
        workspace,
        job,
        version=version,
        blocked_stage=blocked_stage,
        exhausted=False,
        deep=deep,
    )
    return len(validated)


def _validate_fixed_job(job: JobSpec, fixed: FixedEvidenceBundle, *, code: str) -> None:
    if fixed.job_id != job.job_id:
        raise integrity_error(code, "fixed evidence belongs to another job")


def _request_fingerprint(value: BaseModel) -> str:
    payload = value.model_dump(mode="json", exclude={"request_fingerprint"})
    constraints = payload.get("output_constraints")
    if isinstance(constraints, dict) and constraints.get("length") is None:
        constraints.pop("length", None)
        constraints.pop("effective_length", None)
    return fingerprint(payload)


_AuthoringRequestManifest = (
    AuthoringRequestManifest | AuditRequestManifest | RevisionRequestManifest
)


def _request_fingerprint_matches(value: _AuthoringRequestManifest) -> bool:
    return value.request_fingerprint == _request_fingerprint(value)


def _validate_request_fingerprint(value: _AuthoringRequestManifest, *, deep: bool = False) -> None:
    if deep and not _request_fingerprint_matches(value):
        raise integrity_error("AUTHORING_REQUEST_INVALID", "request fingerprint is inconsistent")


def prepare_authoring_request(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    skill_id: str,
) -> AuthoringPrepareResult:
    _validate_fixed_job(job, fixed, code="AUTHORING_REQUEST_STALE")
    if not fixed.evidence_records:
        raise state_error(
            "AUTHORING_EVIDENCE_REQUIRED",
            "authoring requires at least one selected evidence record",
        )
    path = workspace.safe_path("authoring/requests/author-v001.json")
    if path.exists():
        request = _read_model(path, AuthoringRequestManifest, code="AUTHORING_REQUEST_INVALID")
        _validate_request_fingerprint(request)
        if (
            request.job_id != job.job_id
            or not _fixed_evidence_bundle_matches(request.fixed_evidence, fixed)
            or request.output_constraints != _constraints(job, fixed)
            or request.skill.skill_id != skill_id
        ):
            raise integrity_error("AUTHORING_REQUEST_STALE", "authoring request is stale")
        return AuthoringPrepareResult(request=request, reused=True)
    skill = load_skill_snapshot(workspace, skill_id, allowed=_AUTHOR_SKILLS)
    provisional = AuthoringRequestManifest(
        job_id=job.job_id,
        request_fingerprint="0" * 64,
        fixed_evidence=fixed,
        output_constraints=_constraints(job, fixed),
        skill=skill,
        created_at=utc_now(),
    )
    request = provisional.model_copy(
        update={"request_fingerprint": _request_fingerprint(provisional)}
    )
    _write_machine_json(path, request, code="AUTHORING_REQUEST_INVALID")
    return AuthoringPrepareResult(request=request, reused=False)


def load_authoring_request(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    *,
    deep: bool = False,
) -> AuthoringRequestManifest:
    _validate_fixed_job(job, fixed, code="AUTHORING_REQUEST_STALE")
    if not fixed.evidence_records:
        raise state_error(
            "AUTHORING_EVIDENCE_REQUIRED",
            "authoring requires at least one selected evidence record",
        )
    request = _read_model(
        workspace.safe_path("authoring/requests/author-v001.json"),
        AuthoringRequestManifest,
        code="AUTHORING_REQUEST_INVALID",
    )
    _validate_request_fingerprint(request, deep=deep)
    if request.job_id != job.job_id or not _fixed_evidence_bundle_matches(
        request.fixed_evidence, fixed, deep=deep
    ):
        raise integrity_error("AUTHORING_REQUEST_STALE", "authoring request is stale")
    if request.output_constraints != _constraints(job, fixed):
        raise integrity_error("AUTHORING_REQUEST_STALE", "authoring request is stale")
    return request


def _validate_claims(
    claims: tuple[ClaimRecord, ...] | list[ClaimRecord],
    requirement_map: RequirementMap,
    fixed: FixedEvidenceBundle,
) -> None:
    if not fixed.evidence_records:
        raise integrity_error(
            "AUTHORING_RESPONSE_INVALID",
            "authoring requires at least one selected evidence record",
        )
    known_evidence = {item.evidence_id: item for item in fixed.evidence_records}
    known_requirements = {
        item.requirement_id: item.text for item in fixed.evidence_request.assignment.requirements
    }
    claims_by_id = {item.claim_id: item for item in claims}
    if any(
        not set(claim.evidence_ids) <= known_evidence.keys()
        or not set(claim.requirement_ids) <= known_requirements.keys()
        for claim in claims
    ):
        raise integrity_error("AUTHORING_RESPONSE_INVALID", "claim references are invalid")
    for claim in claims:
        records = [known_evidence[item] for item in claim.evidence_ids]
        incompatible = (
            (
                claim.claim_type is ClaimType.VIDEO_FACT
                and any(not item.asserted_as_video_fact for item in records)
            )
            or (
                claim.claim_type is ClaimType.VISUAL_FACT
                and any(
                    item.interpretation_level is not InterpretationLevel.VISUAL_OBSERVATION
                    for item in records
                )
            )
            or (
                claim.claim_type is ClaimType.INFERENCE
                and any(
                    item.interpretation_level is not InterpretationLevel.REASONABLE_INFERENCE
                    for item in records
                )
            )
            or (
                claim.claim_type is ClaimType.EXTERNAL_FACT
                and any(
                    item.interpretation_level is not InterpretationLevel.EXTERNAL_FACT
                    for item in records
                )
            )
        )
        if incompatible:
            raise integrity_error(
                "AUTHORING_RESPONSE_INVALID",
                "claim type is incompatible with its evidence interpretation",
            )
    if [item.requirement_id for item in requirement_map.entries] != list(known_requirements):
        raise integrity_error("AUTHORING_RESPONSE_INVALID", "requirement coverage is incomplete")
    if any(
        item.requirement_text != known_requirements[item.requirement_id]
        or not set(item.claim_ids) <= claims_by_id.keys()
        or not set(item.evidence_ids) <= known_evidence.keys()
        for item in requirement_map.entries
    ):
        raise integrity_error(
            "AUTHORING_RESPONSE_INVALID", "requirement map references are invalid"
        )
    for entry in requirement_map.entries:
        expected_claim_ids = sorted(
            claim.claim_id for claim in claims if entry.requirement_id in claim.requirement_ids
        )
        expected_evidence_ids = sorted(
            {
                evidence_id
                for claim_id in expected_claim_ids
                for evidence_id in claims_by_id[claim_id].evidence_ids
            }
        )
        if entry.claim_ids != expected_claim_ids or entry.evidence_ids != expected_evidence_ids:
            raise integrity_error(
                "AUTHORING_RESPONSE_INVALID",
                "requirement map is not reciprocal with claim lineage",
            )


def read_authoring_response(
    workspace: JobWorkspace,
    path: Path,
    request: AuthoringRequestManifest,
    *,
    allow_fixture: bool,
    enforce_current_skill: bool = True,
) -> AuthoringResponseManifest:
    response = _read_model(path, AuthoringResponseManifest, code="AUTHORING_RESPONSE_INVALID")
    if response.request_fingerprint != request.request_fingerprint:
        raise integrity_error(
            "AUTHORING_RESPONSE_INVALID", "authoring response targets another request"
        )
    _validate_execution(response.author, allow_fixture=allow_fixture)
    if enforce_current_skill:
        _validate_import_skill(
            workspace,
            request.skill,
            response.author,
            allowed=_AUTHOR_SKILLS,
            allow_fixture=allow_fixture,
        )
    _validate_claims(response.claims, response.requirement_map, request.fixed_evidence)
    return response


def _draft_manifest_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"manifests/authoring/draft-{version}.json")


def _draft_stage_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"manifests/stages/authoring-{version}.json")


def _draft_fingerprint(manifest: DraftManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"bundle_fingerprint"})
    if manifest.semantic_revision_count is None:
        payload.pop("semantic_revision_count", None)
        payload.pop("length_adjustment_count", None)
        payload.pop("length_check", None)
    return fingerprint(payload)


def _response_artifact_path(response_fingerprint: str, role: str) -> str:
    return f"authoring/responses/{response_fingerprint}/{role}-response.json"


def _write_attempt_artifact(
    attempt: Path,
    filename: str,
    payload: bytes,
    *,
    artifact_type: str,
    relative_path: str,
) -> tuple[Path, ArtifactRef]:
    if not payload or len(payload) > _MAXIMUM_MACHINE_BYTES:
        raise integrity_error(
            "AUTHORING_OUTPUT_INVALID",
            "authoring output is empty or exceeds the canonical byte limit",
        )
    path = attempt / filename
    atomic_write_bytes(path, payload, private=True)
    return path, _artifact(path, artifact_type=artifact_type, relative_path=relative_path)


def stage_initial_draft(
    workspace: JobWorkspace,
    job: JobSpec,
    request: AuthoringRequestManifest,
    response: AuthoringResponseManifest,
    attempt: Path,
) -> DraftResult:
    if not _valid_attempt(_staging_root(workspace), attempt):
        raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
    _validate_fixed_job(job, request.fixed_evidence, code="AUTHORING_RESPONSE_INVALID")
    if request.job_id != job.job_id or response.request_fingerprint != request.request_fingerprint:
        raise integrity_error("AUTHORING_RESPONSE_INVALID", "authoring response binding is invalid")
    _validate_execution(response.author, allow_fixture=True)
    _validate_claims(response.claims, response.requirement_map, request.fixed_evidence)
    draft_bytes = response.draft_markdown.encode("utf-8")
    if len(draft_bytes) > _MAXIMUM_DRAFT_BYTES:
        raise integrity_error("AUTHORING_OUTPUT_INVALID", "draft exceeds the UTF-8 byte limit")
    response_fingerprint = fingerprint(response)
    version = "v001"
    length_check, length_adjustment_count = _enforce_length_gate(
        workspace,
        job,
        version=version,
        draft_markdown=response.draft_markdown,
        response_fingerprint=response_fingerprint,
        response_payload=pretty_json_bytes(response),
        response_created_at=response.created_at,
        request_fingerprint=response.request_fingerprint,
    )
    base = f"authoring/drafts/{version}"
    draft_path, draft_artifact = _write_attempt_artifact(
        attempt,
        "draft.md",
        draft_bytes,
        artifact_type="draft_markdown",
        relative_path=f"{base}/draft.md",
    )
    claims_path, claims_artifact = _write_attempt_artifact(
        attempt,
        "claims.jsonl",
        _jsonl_bytes(response.claims),
        artifact_type="claim_ledger",
        relative_path=f"{base}/claims.jsonl",
    )
    map_path, map_artifact = _write_attempt_artifact(
        attempt,
        "requirement-map.json",
        pretty_json_bytes(response.requirement_map),
        artifact_type="requirement_map",
        relative_path=f"{base}/requirement-map.json",
    )
    response_path, response_artifact = _write_attempt_artifact(
        attempt,
        "validated-response.json",
        pretty_json_bytes(response),
        artifact_type="validated_authoring_response",
        relative_path=_response_artifact_path(response_fingerprint, "author"),
    )
    started = utc_now()
    run = AgentRunRecord(
        role=AgentRole.AUTHOR,
        skill=request.skill,
        identity=response.author,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        input_artifacts=[
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
            request.fixed_evidence.evidence_manifest.response_artifact,
        ],
        output_artifacts=[draft_artifact, claims_artifact, map_artifact, response_artifact],
        started_at=started,
        finished_at=utc_now(),
    )
    run_path, run_artifact = _write_attempt_artifact(
        attempt,
        "agent-run.json",
        pretty_json_bytes(run),
        artifact_type="agent_run",
        relative_path=f"{base}/agent-run.json",
    )
    provisional = DraftManifest(
        job_id=job.job_id,
        draft_version=version,
        revision_count=0,
        semantic_revision_count=0,
        length_adjustment_count=length_adjustment_count,
        length_check=length_check,
        fixed_evidence_fingerprint=request.fixed_evidence.fixed_evidence_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        bundle_fingerprint="0" * 64,
        producer=response.author,
        draft_artifact=draft_artifact,
        claims_artifact=claims_artifact,
        requirement_map_artifact=map_artifact,
        agent_run_artifact=run_artifact,
        created_at=run.finished_at,
    )
    manifest = provisional.model_copy(
        update={"bundle_fingerprint": _draft_fingerprint(provisional)}
    )
    stage = StageManifest(
        job_id=job.job_id,
        stage="authoring-v001",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.DRAFTED,
        input_artifacts=[
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
            request.fixed_evidence.evidence_manifest.response_artifact,
        ],
        output_artifacts=[
            draft_artifact,
            claims_artifact,
            map_artifact,
            response_artifact,
            run_artifact,
        ],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-authoring-import",
            version=__version__,
        ),
        configuration_sha256=manifest.bundle_fingerprint,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    for path, artifact in (
        (draft_path, draft_artifact),
        (claims_path, claims_artifact),
        (map_path, map_artifact),
        (response_path, response_artifact),
        (run_path, run_artifact),
    ):
        _verify_artifact(path, artifact)
    return DraftResult(
        manifest=manifest,
        stage_manifest=stage,
        response=response,
        claims=tuple(response.claims),
        requirement_map=response.requirement_map,
        agent_run=run,
        reused=False,
    )


def _attempt_draft_artifacts(
    staged: DraftResult, attempt: Path
) -> tuple[tuple[Path, ArtifactRef], ...]:
    return (
        (attempt / "draft.md", staged.manifest.draft_artifact),
        (attempt / "claims.jsonl", staged.manifest.claims_artifact),
        (attempt / "requirement-map.json", staged.manifest.requirement_map_artifact),
        (
            attempt / "validated-response.json",
            next(
                item
                for item in staged.stage_manifest.output_artifacts
                if item.artifact_type.startswith("validated_")
            ),
        ),
        (attempt / "agent-run.json", staged.manifest.agent_run_artifact),
    )


def recover_initial_draft(
    workspace: JobWorkspace,
    job: JobSpec,
    request: AuthoringRequestManifest,
    response: AuthoringResponseManifest,
) -> DraftResult | None:
    """Adopt a fully published author run whose primary manifest was not written.

    The agent-run record is published after all of its content artifacts, so it is
    the durable completion marker for an otherwise orphaned attempt.  Nothing is
    adopted from an incomplete attempt; a mismatched stable artifact fails closed.
    """

    version = "v001"
    base = f"authoring/drafts/{version}"
    run_relative = f"{base}/agent-run.json"
    if not workspace.safe_path(run_relative).exists():
        return None
    response_fingerprint = fingerprint(response)
    draft_artifact = _existing_artifact(
        workspace,
        f"{base}/draft.md",
        artifact_type="draft_markdown",
        code="AUTHORING_ORPHAN_INVALID",
    )
    claims_artifact = _existing_artifact(
        workspace,
        f"{base}/claims.jsonl",
        artifact_type="claim_ledger",
        code="AUTHORING_ORPHAN_INVALID",
    )
    map_artifact = _existing_artifact(
        workspace,
        f"{base}/requirement-map.json",
        artifact_type="requirement_map",
        code="AUTHORING_ORPHAN_INVALID",
    )
    response_artifact = _existing_artifact(
        workspace,
        _response_artifact_path(response_fingerprint, "author"),
        artifact_type="validated_authoring_response",
        code="AUTHORING_ORPHAN_INVALID",
    )
    run_artifact = _existing_artifact(
        workspace,
        run_relative,
        artifact_type="agent_run",
        code="AUTHORING_ORPHAN_INVALID",
    )
    try:
        stored_response = _read_model(
            workspace.safe_path(response_artifact.relative_storage_path),
            AuthoringResponseManifest,
            code="AUTHORING_ORPHAN_INVALID",
        )
        stored_claims = _read_jsonl(
            workspace.safe_path(claims_artifact.relative_storage_path),
            ClaimRecord,
            code="AUTHORING_ORPHAN_INVALID",
        )
        stored_map = _read_model(
            workspace.safe_path(map_artifact.relative_storage_path),
            RequirementMap,
            code="AUTHORING_ORPHAN_INVALID",
        )
        stored_draft = _read_bounded_bytes(
            workspace.safe_path(draft_artifact.relative_storage_path),
            maximum_bytes=_MAXIMUM_DRAFT_BYTES,
            code="AUTHORING_ORPHAN_INVALID",
        ).decode("utf-8")
        run = _read_model(
            workspace.safe_path(run_artifact.relative_storage_path),
            AgentRunRecord,
            code="AUTHORING_ORPHAN_INVALID",
        )
    except UnicodeError:
        raise integrity_error(
            "AUTHORING_ORPHAN_INVALID", "orphan draft is not valid UTF-8"
        ) from None
    expected_inputs = [
        ArtifactRef.model_validate(artifact.model_dump())
        for artifact in (
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
            request.fixed_evidence.evidence_manifest.response_artifact,
        )
    ]
    expected_outputs = [draft_artifact, claims_artifact, map_artifact, response_artifact]
    if (
        stored_response != response
        or stored_draft != response.draft_markdown
        or stored_claims != tuple(response.claims)
        or stored_map != response.requirement_map
        or run.role is not AgentRole.AUTHOR
        or run.skill != request.skill
        or run.identity != response.author
        or run.request_fingerprint != request.request_fingerprint
        or run.response_fingerprint != response_fingerprint
        or run.input_artifacts != expected_inputs
        or run.output_artifacts != expected_outputs
    ):
        raise integrity_error(
            "AUTHORING_ORPHAN_INVALID", "orphan authoring content is inconsistent"
        )
    length_check, length_adjustment_count = _recovered_length_state(
        workspace,
        job,
        version=version,
        draft_markdown=response.draft_markdown,
    )
    provisional = DraftManifest(
        job_id=job.job_id,
        draft_version=version,
        revision_count=0,
        semantic_revision_count=0,
        length_adjustment_count=length_adjustment_count,
        length_check=length_check,
        fixed_evidence_fingerprint=request.fixed_evidence.fixed_evidence_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        bundle_fingerprint="0" * 64,
        producer=response.author,
        draft_artifact=draft_artifact,
        claims_artifact=claims_artifact,
        requirement_map_artifact=map_artifact,
        agent_run_artifact=run_artifact,
        created_at=run.finished_at,
    )
    manifest = provisional.model_copy(
        update={"bundle_fingerprint": _draft_fingerprint(provisional)}
    )
    stage = StageManifest(
        job_id=job.job_id,
        stage="authoring-v001",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.DRAFTED,
        input_artifacts=[
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
            request.fixed_evidence.evidence_manifest.response_artifact,
        ],
        output_artifacts=[*run.output_artifacts, run_artifact],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-authoring-import",
            version=__version__,
        ),
        configuration_sha256=manifest.bundle_fingerprint,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    return DraftResult(
        manifest=manifest,
        stage_manifest=stage,
        response=response,
        claims=stored_claims,
        requirement_map=stored_map,
        agent_run=run,
        reused=False,
    )


def commit_draft(
    workspace: JobWorkspace,
    staged: DraftResult,
    *,
    attempt: Path | None,
) -> DraftResult:
    if attempt is not None:
        if not _valid_attempt(_staging_root(workspace), attempt):
            raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
        for path, artifact in _attempt_draft_artifacts(staged, attempt):
            _publish(workspace, path, artifact)
    else:
        for artifact in (
            staged.manifest.draft_artifact,
            staged.manifest.claims_artifact,
            staged.manifest.requirement_map_artifact,
            staged.manifest.agent_run_artifact,
            next(
                item
                for item in staged.stage_manifest.output_artifacts
                if item.artifact_type.startswith("validated_")
            ),
        ):
            _verify_artifact(workspace.safe_path(artifact.relative_storage_path), artifact)
    _write_or_verify_model(
        _draft_stage_path(workspace, staged.manifest.draft_version),
        staged.stage_manifest,
        StageManifest,
        code="STAGE_MANIFEST_MISMATCH",
    )
    _write_or_verify_model(
        _draft_manifest_path(workspace, staged.manifest.draft_version),
        staged.manifest,
        DraftManifest,
        code="DRAFT_MANIFEST_MISMATCH",
    )
    return staged


def _load_response_for_draft(
    workspace: JobWorkspace,
    manifest: DraftManifest,
    stage: StageManifest,
    *,
    deep: bool = False,
) -> AuthoringResponseManifest | RevisionResponseManifest:
    response_artifacts = [
        item
        for item in stage.output_artifacts
        if item.artifact_type in {"validated_authoring_response", "validated_revision_response"}
    ]
    if len(response_artifacts) != 1:
        raise integrity_error("DRAFT_BUNDLE_INVALID", "draft response artifact is missing")
    artifact = response_artifacts[0]
    path = workspace.safe_path(artifact.relative_storage_path)
    _verify_artifact(path, artifact, deep=deep)
    model: type[AuthoringResponseManifest] | type[RevisionResponseManifest] = (
        AuthoringResponseManifest if manifest.revision_count == 0 else RevisionResponseManifest
    )
    return _read_model(path, model, code="DRAFT_BUNDLE_INVALID")


def load_draft(
    workspace: JobWorkspace,
    job: JobSpec,
    version: str,
    fixed: FixedEvidenceBundle,
    *,
    deep: bool = False,
) -> DraftResult:
    _validate_fixed_job(job, fixed, code="DRAFT_BUNDLE_INVALID")
    manifest = _read_model(
        _draft_manifest_path(workspace, version),
        DraftManifest,
        code="DRAFT_MANIFEST_INVALID",
    )
    stage = _read_model(
        _draft_stage_path(workspace, version),
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    if (
        manifest.job_id != job.job_id
        or manifest.draft_version != version
        or not _fixed_evidence_fingerprint_matches(
            fixed, manifest.fixed_evidence_fingerprint, deep=deep
        )
        or (deep and manifest.bundle_fingerprint != _draft_fingerprint(manifest))
        or stage.job_id != job.job_id
        or (deep and stage.configuration_sha256 != manifest.bundle_fingerprint)
    ):
        raise integrity_error("DRAFT_BUNDLE_INVALID", "draft manifest binding is invalid")
    for artifact in (
        manifest.draft_artifact,
        manifest.claims_artifact,
        manifest.requirement_map_artifact,
        manifest.agent_run_artifact,
        *([manifest.resolution_artifact] if manifest.resolution_artifact is not None else []),
    ):
        _verify_artifact(workspace.safe_path(artifact.relative_storage_path), artifact, deep=deep)
    draft_bytes = _read_bounded_bytes(
        workspace.safe_path(manifest.draft_artifact.relative_storage_path),
        maximum_bytes=_MAXIMUM_DRAFT_BYTES,
        code="DRAFT_BUNDLE_INVALID",
    )
    try:
        draft_markdown = draft_bytes.decode("utf-8")
    except UnicodeError:
        raise integrity_error("DRAFT_BUNDLE_INVALID", "draft is not valid UTF-8") from None
    current_length_check = validate_character_range(
        draft_markdown,
        resolve_length_constraint(job.assignment.length),
    )
    if manifest.length_check != current_length_check or (
        current_length_check is not None and not current_length_check.passed
    ):
        raise integrity_error(
            "DRAFT_BUNDLE_INVALID",
            "draft character-length provenance is missing, stale, or out of range",
        )
    validated_length_adjustments = _validate_successful_length_ledger(
        workspace,
        job,
        version=version,
        blocked_stage=(
            "initial_authoring" if manifest.revision_count == 0 else "semantic_revision"
        ),
        deep=deep,
    )
    if manifest.length_adjustment_count != validated_length_adjustments:
        raise integrity_error(
            "DRAFT_BUNDLE_INVALID", "draft length-adjustment count is not bound to its ledger"
        )
    claims = _read_jsonl(
        workspace.safe_path(manifest.claims_artifact.relative_storage_path),
        ClaimRecord,
        code="DRAFT_BUNDLE_INVALID",
    )
    requirement_map = _read_model(
        workspace.safe_path(manifest.requirement_map_artifact.relative_storage_path),
        RequirementMap,
        code="DRAFT_BUNDLE_INVALID",
    )
    run = _read_model(
        workspace.safe_path(manifest.agent_run_artifact.relative_storage_path),
        AgentRunRecord,
        code="DRAFT_BUNDLE_INVALID",
    )
    response = _load_response_for_draft(workspace, manifest, stage, deep=deep)
    response_artifact = next(
        item
        for item in stage.output_artifacts
        if item.artifact_type in {"validated_authoring_response", "validated_revision_response"}
    )
    response_producer = (
        response.author if isinstance(response, AuthoringResponseManifest) else response.reviser
    )
    _validate_execution(response_producer, allow_fixture=True)
    initial_request = (
        load_authoring_request(workspace, job, fixed, deep=deep)
        if manifest.revision_count == 0
        else None
    )
    stage_kind = "authoring" if manifest.revision_count == 0 else "revision"
    expected_stage_name = f"{stage_kind}-{version}"
    expected_producer = ProducerIdentity(
        kind=ProducerKind.TOOL,
        name=f"video-evidence-pipeline-{stage_kind}-import",
        version=__version__,
    )
    if (
        response.draft_markdown != draft_markdown
        or tuple(response.claims) != claims
        or response.requirement_map != requirement_map
        or response_producer != manifest.producer
        or run.identity != manifest.producer
        or (initial_request is not None and (run.skill != initial_request.skill))
        or stage.stage != expected_stage_name
        or stage.status is not StageStatus.SUCCEEDED
        or stage.state_transition is not JobState.DRAFTED
        or _artifact_payloads(stage.input_artifacts) != _artifact_payloads(run.input_artifacts)
        or _artifact_payloads(stage.output_artifacts)
        != _artifact_payloads([*run.output_artifacts, manifest.agent_run_artifact])
        or response_artifact not in run.output_artifacts
        or stage.producer != expected_producer
        or stage.started_at != run.started_at
        or stage.finished_at != run.finished_at
        or (
            deep
            and (
                response.request_fingerprint != manifest.request_fingerprint
                or fingerprint(response) != manifest.response_fingerprint
                or run.response_fingerprint != manifest.response_fingerprint
                or run.request_fingerprint != manifest.request_fingerprint
                or (
                    initial_request is not None
                    and manifest.request_fingerprint != initial_request.request_fingerprint
                )
            )
        )
    ):
        raise integrity_error("DRAFT_BUNDLE_INVALID", "draft contents are inconsistent")
    _validate_claims(claims, requirement_map, fixed)
    return DraftResult(
        manifest=manifest,
        stage_manifest=stage,
        response=response,
        claims=claims,
        requirement_map=requirement_map,
        agent_run=run,
        reused=True,
    )


def _audit_request_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"authoring/requests/audit-{version}.json")


def _audit_manifest_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"manifests/authoring/audit-{version}.json")


def _audit_stage_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"manifests/stages/audit-{version}.json")


def _audit_fingerprint(manifest: AuditManifest) -> str:
    return fingerprint(manifest.model_dump(mode="json", exclude={"bundle_fingerprint"}))


def _draft_text(workspace: JobWorkspace, draft: DraftResult) -> str:
    draft_path = workspace.safe_path(draft.manifest.draft_artifact.relative_storage_path)
    _verify_artifact(draft_path, draft.manifest.draft_artifact)
    payload = _read_bounded_bytes(
        draft_path,
        maximum_bytes=_MAXIMUM_DRAFT_BYTES,
        code="DRAFT_BUNDLE_INVALID",
    )
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        raise integrity_error("DRAFT_BUNDLE_INVALID", "draft is not valid UTF-8") from None


def _validate_audit_target_lineage(
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    *,
    code: str,
    deep: bool = False,
) -> None:
    _validate_fixed_job(job, fixed, code=code)
    if (
        draft.manifest.job_id != job.job_id
        or not _fixed_evidence_fingerprint_matches(
            fixed, draft.manifest.fixed_evidence_fingerprint, deep=deep
        )
        or draft.manifest.draft_version != f"v{draft.manifest.revision_count + 1:03d}"
    ):
        raise integrity_error(code, "audit target lineage is invalid")


def prepare_audit_request(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    skill_id: str,
) -> AuditPrepareResult:
    _validate_audit_target_lineage(
        job,
        fixed,
        draft,
        code="AUDIT_REQUEST_STALE",
    )
    text = _draft_text(workspace, draft)
    path = _audit_request_path(workspace, draft.manifest.draft_version)
    if path.exists():
        request = _read_model(path, AuditRequestManifest, code="AUDIT_REQUEST_INVALID")
        _validate_request_fingerprint(request)
        if (
            request.job_id != job.job_id
            or not _fixed_evidence_bundle_matches(request.fixed_evidence, fixed)
            or request.target_draft_sha256 != draft.manifest.draft_artifact.content_sha256
            or request.target_draft_markdown != text
            or request.skill.skill_id != skill_id
            or request.output_constraints != _constraints(job, fixed)
        ):
            raise integrity_error("AUDIT_REQUEST_STALE", "audit request is stale")
        return AuditPrepareResult(request=request, reused=True)
    skill = load_skill_snapshot(workspace, skill_id, allowed=_AUDIT_SKILLS)
    provisional = AuditRequestManifest(
        job_id=job.job_id,
        request_fingerprint="0" * 64,
        target_draft_version=draft.manifest.draft_version,
        target_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        target_draft_artifact=draft.manifest.draft_artifact,
        target_draft_markdown=text,
        fixed_evidence=fixed,
        output_constraints=_constraints(job, fixed),
        skill=skill,
        created_at=utc_now(),
    )
    request = provisional.model_copy(
        update={"request_fingerprint": _request_fingerprint(provisional)}
    )
    _write_machine_json(path, request, code="AUDIT_REQUEST_INVALID")
    return AuditPrepareResult(request=request, reused=False)


def load_audit_request(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    *,
    deep: bool = False,
) -> AuditRequestManifest:
    _validate_audit_target_lineage(
        job,
        fixed,
        draft,
        code="AUDIT_REQUEST_STALE",
        deep=deep,
    )
    request = _read_model(
        _audit_request_path(workspace, draft.manifest.draft_version),
        AuditRequestManifest,
        code="AUDIT_REQUEST_INVALID",
    )
    _validate_request_fingerprint(request, deep=deep)
    if (
        request.job_id != job.job_id
        or not _fixed_evidence_bundle_matches(request.fixed_evidence, fixed, deep=deep)
        or request.target_draft_artifact != draft.manifest.draft_artifact
        or request.target_draft_markdown != _draft_text(workspace, draft)
        or request.output_constraints != _constraints(job, fixed)
    ):
        raise integrity_error("AUDIT_REQUEST_STALE", "audit request is stale")
    return request


def _source_ref_catalog(
    fixed: FixedEvidenceBundle,
) -> set[tuple[str, str, int | None, int | None, str]]:
    request = fixed.evidence_request
    return {
        (
            item.kind.value,
            item.ref_id,
            item.start_ms,
            item.end_ms,
            item.content_sha256,
        )
        for item in (request.transcript_refs + request.frame_refs + request.approved_artifact_refs)
    }


def _source_ref_key(item: SourceRef) -> tuple[str, str, int | None, int | None, str]:
    return (
        item.kind.value,
        item.ref_id,
        item.start_ms,
        item.end_ms,
        item.content_sha256,
    )


def _validate_audit_response_semantics(
    response: AuditResponseManifest,
    request: AuditRequestManifest,
    *,
    forbidden_context_ids: tuple[str, ...],
    check_fingerprints: bool = True,
) -> None:
    if check_fingerprints and (
        response.request_fingerprint != request.request_fingerprint
        or response.target_draft_sha256 != request.target_draft_sha256
    ):
        raise integrity_error("AUDIT_RESPONSE_INVALID", "audit response targets another request")
    if response.auditor.context_id in forbidden_context_ids:
        raise integrity_error("AUDIT_CONTEXT_NOT_INDEPENDENT", "auditor context is not independent")
    evidence_by_id = {item.evidence_id: item for item in request.fixed_evidence.evidence_records}
    known_evidence = set(evidence_by_id)
    known_requirements = {item.requirement_id for item in request.output_constraints.requirements}
    known_sources = _source_ref_catalog(request.fixed_evidence)
    known_audit_claims = {item.audit_claim_id for item in response.audit_claims}
    for claim in response.audit_claims:
        if claim.draft_span.end > len(request.target_draft_markdown) or (
            request.target_draft_markdown[claim.draft_span.start : claim.draft_span.end]
            != claim.draft_span.text
        ):
            raise integrity_error("AUDIT_RESPONSE_INVALID", "audit claim span is invalid")
        if not set(claim.evidence_ids) <= known_evidence:
            raise integrity_error("AUDIT_RESPONSE_INVALID", "audit claim evidence is invalid")
        records = [
            item
            for item in request.fixed_evidence.evidence_records
            if item.evidence_id in claim.evidence_ids
        ]
        incompatible = (
            (
                claim.claim_type is ClaimType.VIDEO_FACT
                and any(not item.asserted_as_video_fact for item in records)
            )
            or (
                claim.claim_type is ClaimType.VISUAL_FACT
                and any(
                    item.interpretation_level is not InterpretationLevel.VISUAL_OBSERVATION
                    for item in records
                )
            )
            or (
                claim.claim_type is ClaimType.INFERENCE
                and any(
                    item.interpretation_level is not InterpretationLevel.REASONABLE_INFERENCE
                    for item in records
                )
            )
            or (
                claim.claim_type is ClaimType.EXTERNAL_FACT
                and any(
                    item.interpretation_level is not InterpretationLevel.EXTERNAL_FACT
                    for item in records
                )
            )
        )
        if incompatible:
            raise integrity_error(
                "AUDIT_RESPONSE_INVALID",
                "audit claim type is incompatible with its evidence interpretation",
            )
    for finding in response.findings:
        if (
            not set(finding.audit_claim_ids) <= known_audit_claims
            or not set(finding.requirement_ids) <= known_requirements
            or not set(finding.evidence_ids) <= known_evidence
        ):
            raise integrity_error("AUDIT_RESPONSE_INVALID", "audit finding references are invalid")
        if finding.draft_span is not None and (
            finding.draft_span.end > len(request.target_draft_markdown)
            or request.target_draft_markdown[finding.draft_span.start : finding.draft_span.end]
            != finding.draft_span.text
        ):
            raise integrity_error("AUDIT_RESPONSE_INVALID", "audit finding span is invalid")
        evidence_sources = {
            _source_ref_key(source)
            for evidence_id in finding.evidence_ids
            for source in evidence_by_id[evidence_id].source_refs
        }
        source_keys = [_source_ref_key(item) for item in finding.source_refs]
        if (
            any(key not in known_sources for key in source_keys)
            or (finding.evidence_ids and source_keys != sorted(evidence_sources))
            or (not finding.evidence_ids and source_keys)
        ):
            raise integrity_error("AUDIT_RESPONSE_INVALID", "audit source reference is invalid")


def read_audit_response(
    workspace: JobWorkspace,
    path: Path,
    request: AuditRequestManifest,
    *,
    forbidden_context_ids: tuple[str, ...],
    allow_fixture: bool,
    enforce_current_skill: bool = True,
) -> AuditResponseManifest:
    response = _read_model(path, AuditResponseManifest, code="AUDIT_RESPONSE_INVALID")
    _validate_execution(response.auditor, allow_fixture=allow_fixture)
    if enforce_current_skill:
        _validate_import_skill(
            workspace,
            request.skill,
            response.auditor,
            allowed=_AUDIT_SKILLS,
            allow_fixture=allow_fixture,
        )
    _validate_audit_response_semantics(
        response,
        request,
        forbidden_context_ids=forbidden_context_ids,
    )
    return response


def _audit_disposition(draft: DraftResult, findings: list[AuditFinding]) -> AuditDisposition:
    material = any(item.severity in {AuditSeverity.P0, AuditSeverity.P1} for item in findings)
    if not material:
        return AuditDisposition.HUMAN_REVIEW_READY
    if draft.manifest.revision_count >= 2:
        return AuditDisposition.BLOCKED
    return AuditDisposition.REVISION_REQUIRED


def compare_author_and_audit_claims(
    author_claims: tuple[ClaimRecord, ...],
    audit_claims: list[AuditClaimRecord] | tuple[AuditClaimRecord, ...],
) -> tuple[ClaimComparisonRecord, ...]:
    """Compare validated claims by exact Unicode draft span in canonical order."""

    remaining = list(audit_claims)
    provisional: list[dict[str, object]] = []
    for author_claim in author_claims:
        same_span = [item for item in remaining if item.draft_span == author_claim.draft_span]
        exact = next(
            (
                item
                for item in same_span
                if item.claim_type is author_claim.claim_type
                and item.evidence_ids == author_claim.evidence_ids
            ),
            None,
        )
        paired = exact or (same_span[0] if same_span else None)
        if paired is None:
            provisional.append(
                {
                    "status": ClaimComparisonStatus.AUTHOR_ONLY,
                    "draft_span": author_claim.draft_span,
                    "author_claim_id": author_claim.claim_id,
                }
            )
            continue
        remaining.remove(paired)
        differences: list[str] = []
        if paired.claim_type is not author_claim.claim_type:
            differences.append("claim_type")
        if paired.evidence_ids != author_claim.evidence_ids:
            differences.append("evidence_ids")
        provisional.append(
            {
                "status": (
                    ClaimComparisonStatus.CHANGED if differences else ClaimComparisonStatus.MATCHED
                ),
                "draft_span": author_claim.draft_span,
                "author_claim_id": author_claim.claim_id,
                "audit_claim_id": paired.audit_claim_id,
                "differences": sorted(differences),
            }
        )
    for audit_claim in remaining:
        provisional.append(
            {
                "status": ClaimComparisonStatus.AUDIT_ONLY,
                "draft_span": audit_claim.draft_span,
                "audit_claim_id": audit_claim.audit_claim_id,
            }
        )
    provisional.sort(
        key=lambda item: (
            item["draft_span"].start,  # type: ignore[union-attr]
            item["draft_span"].end,  # type: ignore[union-attr]
            str(item.get("author_claim_id", "")),
            str(item.get("audit_claim_id", "")),
        )
    )
    return tuple(
        ClaimComparisonRecord(comparison_id=f"CP{index:06d}", **item)
        for index, item in enumerate(provisional, start=1)
    )


def stage_audit(
    workspace: JobWorkspace,
    job: JobSpec,
    request: AuditRequestManifest,
    response: AuditResponseManifest,
    draft: DraftResult,
    history: AuthoringHistory,
    attempt: Path,
) -> AuditResult:
    if not _valid_attempt(_staging_root(workspace), attempt):
        raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
    if not history or history[-1][1] is not None:
        raise integrity_error("AUDIT_REQUEST_STALE", "audit target history is not pending")
    authoring_context_ids, prior_auditor_context_ids = _validated_history_contexts(
        history,
        job=job,
        fixed=request.fixed_evidence,
        latest_draft=draft,
    )
    if (
        request.job_id != job.job_id
        or request.target_draft_version != draft.manifest.draft_version
        or not _fixed_evidence_fingerprint_matches(
            request.fixed_evidence, draft.manifest.fixed_evidence_fingerprint
        )
        or response.request_fingerprint != request.request_fingerprint
        or draft.manifest.job_id != job.job_id
    ):
        raise integrity_error("AUDIT_REQUEST_STALE", "audit inputs are not bound")
    if response.auditor.context_id in {*authoring_context_ids, *prior_auditor_context_ids}:
        raise integrity_error(
            "AUDIT_CONTEXT_NOT_INDEPENDENT",
            "audit context is not bound to the authoring history",
        )
    _validate_audit_response_semantics(
        response,
        request,
        forbidden_context_ids=tuple(sorted({*authoring_context_ids, *prior_auditor_context_ids})),
    )
    _validate_execution(response.auditor, allow_fixture=True)
    if request.target_draft_sha256 != draft.manifest.draft_artifact.content_sha256:
        raise integrity_error("AUDIT_REQUEST_STALE", "audit target draft is stale")
    response_fingerprint = fingerprint(response)
    version = draft.manifest.draft_version
    base = f"authoring/audits/{version}"
    claims_path, claims_artifact = _write_attempt_artifact(
        attempt,
        "audit-claims.jsonl",
        _jsonl_bytes(response.audit_claims),
        artifact_type="audit_claim_ledger",
        relative_path=f"{base}/audit-claims.jsonl",
    )
    comparisons = compare_author_and_audit_claims(draft.claims, response.audit_claims)
    comparison_path, comparison_artifact = _write_attempt_artifact(
        attempt,
        "claim-comparison.jsonl",
        _jsonl_bytes(comparisons),
        artifact_type="claim_comparison_ledger",
        relative_path=f"{base}/claim-comparison.jsonl",
    )
    findings_path, findings_artifact = _write_attempt_artifact(
        attempt,
        "audit-findings.json",
        (
            json.dumps(
                [item.model_dump(mode="json") for item in response.findings],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
        artifact_type="audit_findings",
        relative_path=f"{base}/findings.json",
    )
    response_path, response_artifact = _write_attempt_artifact(
        attempt,
        "validated-audit-response.json",
        pretty_json_bytes(response),
        artifact_type="validated_audit_response",
        relative_path=_response_artifact_path(response_fingerprint, "audit"),
    )
    started = utc_now()
    run = AgentRunRecord(
        role=AgentRole.AUDITOR,
        skill=request.skill,
        identity=response.auditor,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        input_artifacts=[
            draft.manifest.draft_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
        ],
        output_artifacts=[
            claims_artifact,
            comparison_artifact,
            findings_artifact,
            response_artifact,
        ],
        started_at=started,
        finished_at=utc_now(),
    )
    run_path, run_artifact = _write_attempt_artifact(
        attempt,
        "audit-agent-run.json",
        pretty_json_bytes(run),
        artifact_type="agent_run",
        relative_path=f"{base}/agent-run.json",
    )
    p0 = sum(item.severity is AuditSeverity.P0 for item in response.findings)
    p1 = sum(item.severity is AuditSeverity.P1 for item in response.findings)
    p2 = sum(item.severity is AuditSeverity.P2 for item in response.findings)
    disposition = _audit_disposition(draft, response.findings)
    provisional = AuditManifest(
        job_id=job.job_id,
        target_draft_version=version,
        target_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        fixed_evidence_fingerprint=request.fixed_evidence.fixed_evidence_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        bundle_fingerprint="0" * 64,
        authoring_context_ids=list(authoring_context_ids),
        prior_auditor_context_ids=list(prior_auditor_context_ids),
        auditor=response.auditor,
        audit_claims_artifact=claims_artifact,
        claim_comparison_artifact=comparison_artifact,
        findings_artifact=findings_artifact,
        response_artifact=response_artifact,
        agent_run_artifact=run_artifact,
        p0_count=p0,
        p1_count=p1,
        p2_count=p2,
        claim_comparison_count=len(comparisons),
        disposition=disposition,
        created_at=run.finished_at,
    )
    manifest = provisional.model_copy(
        update={"bundle_fingerprint": _audit_fingerprint(provisional)}
    )
    stage = StageManifest(
        job_id=job.job_id,
        stage=f"audit-{version}",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.AUDITED,
        input_artifacts=[
            draft.manifest.draft_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
        ],
        output_artifacts=[
            claims_artifact,
            comparison_artifact,
            findings_artifact,
            response_artifact,
            run_artifact,
        ],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-audit-import",
            version=__version__,
        ),
        configuration_sha256=manifest.bundle_fingerprint,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    for path, artifact in (
        (claims_path, claims_artifact),
        (comparison_path, comparison_artifact),
        (findings_path, findings_artifact),
        (response_path, response_artifact),
        (run_path, run_artifact),
    ):
        _verify_artifact(path, artifact)
    return AuditResult(
        manifest=manifest,
        stage_manifest=stage,
        response=response,
        audit_claims=tuple(response.audit_claims),
        claim_comparisons=comparisons,
        findings=tuple(response.findings),
        agent_run=run,
        human_review=None,
        reused=False,
    )


def recover_audit(
    workspace: JobWorkspace,
    job: JobSpec,
    request: AuditRequestManifest,
    response: AuditResponseManifest,
    draft: DraftResult,
    history: AuthoringHistory,
) -> AuditResult | None:
    """Rebuild audit manifests from a complete stable run after an interruption."""

    version = draft.manifest.draft_version
    base = f"authoring/audits/{version}"
    run_relative = f"{base}/agent-run.json"
    if not workspace.safe_path(run_relative).exists():
        return None
    if not history or history[-1][1] is not None:
        raise integrity_error("AUDIT_ORPHAN_INVALID", "orphan audit history is not pending")
    authoring_context_ids, prior_auditor_context_ids = _validated_history_contexts(
        history,
        job=job,
        fixed=request.fixed_evidence,
        latest_draft=draft,
    )
    _validate_audit_response_semantics(
        response,
        request,
        forbidden_context_ids=tuple(sorted({*authoring_context_ids, *prior_auditor_context_ids})),
    )
    response_fingerprint = fingerprint(response)
    claims_artifact = _existing_artifact(
        workspace,
        f"{base}/audit-claims.jsonl",
        artifact_type="audit_claim_ledger",
        code="AUDIT_ORPHAN_INVALID",
    )
    comparison_artifact = _existing_artifact(
        workspace,
        f"{base}/claim-comparison.jsonl",
        artifact_type="claim_comparison_ledger",
        code="AUDIT_ORPHAN_INVALID",
    )
    findings_artifact = _existing_artifact(
        workspace,
        f"{base}/findings.json",
        artifact_type="audit_findings",
        code="AUDIT_ORPHAN_INVALID",
    )
    response_artifact = _existing_artifact(
        workspace,
        _response_artifact_path(response_fingerprint, "audit"),
        artifact_type="validated_audit_response",
        code="AUDIT_ORPHAN_INVALID",
    )
    run_artifact = _existing_artifact(
        workspace,
        run_relative,
        artifact_type="agent_run",
        code="AUDIT_ORPHAN_INVALID",
    )
    stored_response = _read_model(
        workspace.safe_path(response_artifact.relative_storage_path),
        AuditResponseManifest,
        code="AUDIT_ORPHAN_INVALID",
    )
    stored_claims = _read_jsonl(
        workspace.safe_path(claims_artifact.relative_storage_path),
        AuditClaimRecord,
        code="AUDIT_ORPHAN_INVALID",
    )
    stored_comparisons = _read_jsonl(
        workspace.safe_path(comparison_artifact.relative_storage_path),
        ClaimComparisonRecord,
        code="AUDIT_ORPHAN_INVALID",
    )
    stored_findings = _read_json_list(
        workspace.safe_path(findings_artifact.relative_storage_path),
        AuditFinding,
        code="AUDIT_ORPHAN_INVALID",
    )
    run = _read_model(
        workspace.safe_path(run_artifact.relative_storage_path),
        AgentRunRecord,
        code="AUDIT_ORPHAN_INVALID",
    )
    comparisons = compare_author_and_audit_claims(draft.claims, response.audit_claims)
    expected_inputs = [
        ArtifactRef.model_validate(artifact.model_dump())
        for artifact in (
            draft.manifest.draft_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
        )
    ]
    expected_outputs = [
        claims_artifact,
        comparison_artifact,
        findings_artifact,
        response_artifact,
    ]
    if (
        stored_response != response
        or stored_claims != tuple(response.audit_claims)
        or stored_comparisons != comparisons
        or stored_findings != tuple(response.findings)
        or run.role is not AgentRole.AUDITOR
        or run.skill != request.skill
        or run.identity != response.auditor
        or run.request_fingerprint != request.request_fingerprint
        or run.response_fingerprint != response_fingerprint
        or run.input_artifacts != expected_inputs
        or run.output_artifacts != expected_outputs
    ):
        raise integrity_error("AUDIT_ORPHAN_INVALID", "orphan audit content is inconsistent")
    p0 = sum(item.severity is AuditSeverity.P0 for item in response.findings)
    p1 = sum(item.severity is AuditSeverity.P1 for item in response.findings)
    p2 = sum(item.severity is AuditSeverity.P2 for item in response.findings)
    provisional = AuditManifest(
        job_id=job.job_id,
        target_draft_version=version,
        target_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        fixed_evidence_fingerprint=request.fixed_evidence.fixed_evidence_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        bundle_fingerprint="0" * 64,
        authoring_context_ids=list(authoring_context_ids),
        prior_auditor_context_ids=list(prior_auditor_context_ids),
        auditor=response.auditor,
        audit_claims_artifact=claims_artifact,
        claim_comparison_artifact=comparison_artifact,
        findings_artifact=findings_artifact,
        response_artifact=response_artifact,
        agent_run_artifact=run_artifact,
        p0_count=p0,
        p1_count=p1,
        p2_count=p2,
        claim_comparison_count=len(comparisons),
        disposition=_audit_disposition(draft, list(response.findings)),
        created_at=run.finished_at,
    )
    manifest = provisional.model_copy(
        update={"bundle_fingerprint": _audit_fingerprint(provisional)}
    )
    stage = StageManifest(
        job_id=job.job_id,
        stage=f"audit-{version}",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.AUDITED,
        input_artifacts=[
            draft.manifest.draft_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
            request.fixed_evidence.evidence_manifest.ambiguity_artifact,
        ],
        output_artifacts=[*run.output_artifacts, run_artifact],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-audit-import",
            version=__version__,
        ),
        configuration_sha256=manifest.bundle_fingerprint,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    return AuditResult(
        manifest=manifest,
        stage_manifest=stage,
        response=response,
        audit_claims=stored_claims,
        claim_comparisons=stored_comparisons,
        findings=stored_findings,
        agent_run=run,
        human_review=None,
        reused=False,
    )


def commit_audit(
    workspace: JobWorkspace,
    staged: AuditResult,
    *,
    attempt: Path | None,
) -> AuditResult:
    artifacts = (
        ("audit-claims.jsonl", staged.manifest.audit_claims_artifact),
        ("claim-comparison.jsonl", staged.manifest.claim_comparison_artifact),
        ("audit-findings.json", staged.manifest.findings_artifact),
        ("validated-audit-response.json", staged.manifest.response_artifact),
        ("audit-agent-run.json", staged.manifest.agent_run_artifact),
    )
    if attempt is not None:
        if not _valid_attempt(_staging_root(workspace), attempt):
            raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
        for filename, artifact in artifacts:
            _publish(workspace, attempt / filename, artifact)
    else:
        for _filename, artifact in artifacts:
            _verify_artifact(workspace.safe_path(artifact.relative_storage_path), artifact)
    version = staged.manifest.target_draft_version
    _write_or_verify_model(
        _audit_stage_path(workspace, version),
        staged.stage_manifest,
        StageManifest,
        code="STAGE_MANIFEST_MISMATCH",
    )
    _write_or_verify_model(
        _audit_manifest_path(workspace, version),
        staged.manifest,
        AuditManifest,
        code="AUDIT_MANIFEST_MISMATCH",
    )
    return staged


def load_audit(
    workspace: JobWorkspace,
    job: JobSpec,
    draft: DraftResult,
    fixed: FixedEvidenceBundle,
    history: AuthoringHistory,
    *,
    deep: bool = False,
) -> AuditResult:
    if not history or history[-1][1] is not None:
        raise integrity_error("AUDIT_BUNDLE_INVALID", "audit load history is not pending")
    authoring_context_ids, prior_auditor_context_ids = _validated_history_contexts(
        history,
        job=job,
        fixed=fixed,
        latest_draft=draft,
        deep=deep,
    )
    version = draft.manifest.draft_version
    request = load_audit_request(workspace, job, fixed, draft, deep=deep)
    manifest = _read_model(
        _audit_manifest_path(workspace, version),
        AuditManifest,
        code="AUDIT_MANIFEST_INVALID",
    )
    stage = _read_model(
        _audit_stage_path(workspace, version),
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    if (
        manifest.job_id != job.job_id
        or manifest.target_draft_version != version
        or manifest.target_draft_sha256 != draft.manifest.draft_artifact.content_sha256
        or manifest.authoring_context_ids != list(authoring_context_ids)
        or manifest.prior_auditor_context_ids != list(prior_auditor_context_ids)
        or (
            deep
            and not _fixed_evidence_fingerprint_matches(
                fixed, manifest.fixed_evidence_fingerprint, deep=True
            )
        )
        or (deep and manifest.request_fingerprint != request.request_fingerprint)
        or (deep and manifest.bundle_fingerprint != _audit_fingerprint(manifest))
        or stage.job_id != job.job_id
        or (deep and stage.configuration_sha256 != manifest.bundle_fingerprint)
    ):
        raise integrity_error("AUDIT_BUNDLE_INVALID", "audit manifest binding is invalid")
    for artifact in (
        manifest.audit_claims_artifact,
        manifest.claim_comparison_artifact,
        manifest.findings_artifact,
        manifest.response_artifact,
        manifest.agent_run_artifact,
    ):
        _verify_artifact(workspace.safe_path(artifact.relative_storage_path), artifact, deep=deep)
    claims = _read_jsonl(
        workspace.safe_path(manifest.audit_claims_artifact.relative_storage_path),
        AuditClaimRecord,
        code="AUDIT_BUNDLE_INVALID",
    )
    comparisons = _read_jsonl(
        workspace.safe_path(manifest.claim_comparison_artifact.relative_storage_path),
        ClaimComparisonRecord,
        code="AUDIT_BUNDLE_INVALID",
    )
    findings = _read_json_list(
        workspace.safe_path(manifest.findings_artifact.relative_storage_path),
        AuditFinding,
        code="AUDIT_BUNDLE_INVALID",
    )
    response = _read_model(
        workspace.safe_path(manifest.response_artifact.relative_storage_path),
        AuditResponseManifest,
        code="AUDIT_BUNDLE_INVALID",
    )
    _validate_execution(response.auditor, allow_fixture=True)
    _validate_audit_response_semantics(
        response,
        request,
        forbidden_context_ids=tuple(sorted({*authoring_context_ids, *prior_auditor_context_ids})),
        check_fingerprints=deep,
    )
    run = _read_model(
        workspace.safe_path(manifest.agent_run_artifact.relative_storage_path),
        AgentRunRecord,
        code="AUDIT_BUNDLE_INVALID",
    )
    if (
        tuple(response.audit_claims) != claims
        or comparisons != compare_author_and_audit_claims(draft.claims, response.audit_claims)
        or tuple(response.findings) != findings
        or response.target_draft_sha256 != manifest.target_draft_sha256
        or response.auditor != manifest.auditor
        or (deep and run.response_fingerprint != manifest.response_fingerprint)
        or (deep and run.request_fingerprint != manifest.request_fingerprint)
        or run.identity != manifest.auditor
        or run.skill != request.skill
        or run.role is not AgentRole.AUDITOR
        or stage.stage != f"audit-{version}"
        or stage.status is not StageStatus.SUCCEEDED
        or stage.state_transition is not JobState.AUDITED
        or _artifact_payloads(stage.input_artifacts) != _artifact_payloads(run.input_artifacts)
        or _artifact_payloads(stage.output_artifacts)
        != _artifact_payloads([*run.output_artifacts, manifest.agent_run_artifact])
        or stage.producer
        != ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-audit-import",
            version=__version__,
        )
        or stage.started_at != run.started_at
        or stage.finished_at != run.finished_at
        or manifest.p0_count != sum(item.severity is AuditSeverity.P0 for item in findings)
        or manifest.p1_count != sum(item.severity is AuditSeverity.P1 for item in findings)
        or manifest.p2_count != sum(item.severity is AuditSeverity.P2 for item in findings)
        or manifest.claim_comparison_count != len(comparisons)
        or manifest.disposition != _audit_disposition(draft, list(findings))
        or (
            deep
            and (
                fingerprint(response) != manifest.response_fingerprint
                or response.request_fingerprint != manifest.request_fingerprint
                or run.response_fingerprint != manifest.response_fingerprint
                or run.request_fingerprint != manifest.request_fingerprint
            )
        )
    ):
        raise integrity_error("AUDIT_BUNDLE_INVALID", "audit contents are inconsistent")
    return AuditResult(
        manifest=manifest,
        stage_manifest=stage,
        response=response,
        audit_claims=claims,
        claim_comparisons=comparisons,
        findings=findings,
        agent_run=run,
        human_review=None,
        reused=True,
    )


def _revision_request_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"authoring/requests/revision-{version}.json")


def _revision_manifest_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"manifests/authoring/revision-{version}.json")


def _revision_fingerprint(manifest: RevisionManifest) -> str:
    return fingerprint(manifest.model_dump(mode="json", exclude={"bundle_fingerprint"}))


def _validate_revision_lineage(
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    *,
    code: str,
    deep: bool = False,
) -> None:
    if (
        fixed.job_id != job.job_id
        or draft.manifest.job_id != job.job_id
        or not _fixed_evidence_fingerprint_matches(
            fixed, draft.manifest.fixed_evidence_fingerprint, deep=deep
        )
        or audit.manifest.job_id != job.job_id
        or not _fixed_evidence_fingerprint_matches(
            fixed, audit.manifest.fixed_evidence_fingerprint, deep=deep
        )
        or audit.manifest.target_draft_version != draft.manifest.draft_version
        or audit.manifest.target_draft_sha256 != draft.manifest.draft_artifact.content_sha256
        or audit.manifest.disposition is not AuditDisposition.REVISION_REQUIRED
    ):
        raise integrity_error(code, "revision predecessor lineage is invalid")


def _revision_request_is_bound(
    workspace: JobWorkspace,
    request: RevisionRequestManifest,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    *,
    deep: bool = False,
) -> bool:
    expected_count = draft.manifest.revision_count + 1
    return (
        request.job_id == job.job_id
        and _fixed_evidence_bundle_matches(request.fixed_evidence, fixed, deep=deep)
        and request.target_draft_version == draft.manifest.draft_version
        and request.next_draft_version == f"v{expected_count + 1:03d}"
        and request.revision_count == expected_count
        and request.target_draft_sha256 == draft.manifest.draft_artifact.content_sha256
        and request.target_draft_artifact == draft.manifest.draft_artifact
        and request.target_draft_markdown == _draft_text(workspace, draft)
        and request.author_claims == list(draft.claims)
        and request.requirement_map == draft.requirement_map
        and (not deep or request.audit_bundle_fingerprint == audit.manifest.bundle_fingerprint)
        and request.audit_finding_artifact == audit.manifest.findings_artifact
        and request.audit_findings == list(audit.findings)
        and request.output_constraints == _constraints(job, fixed)
    )


def prepare_revision_request(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    skill_id: str,
) -> RevisionPrepareResult:
    _validate_revision_lineage(
        job,
        fixed,
        draft,
        audit,
        code="REVISION_REQUEST_STALE",
    )
    next_count = draft.manifest.revision_count + 1
    if next_count > 2:
        raise state_error("REVISION_LIMIT_REACHED", "automatic revision limit is exhausted")
    next_version = f"v{next_count + 1:03d}"
    path = _revision_request_path(workspace, next_version)
    if path.exists():
        request = _read_model(path, RevisionRequestManifest, code="REVISION_REQUEST_INVALID")
        _validate_request_fingerprint(request)
        if (
            not _revision_request_is_bound(workspace, request, job, fixed, draft, audit)
            or request.skill.skill_id != skill_id
        ):
            raise integrity_error("REVISION_REQUEST_STALE", "revision request is stale")
        return RevisionPrepareResult(request=request, reused=True)
    skill = load_skill_snapshot(workspace, skill_id, allowed=_REVISION_SKILLS)
    provisional = RevisionRequestManifest(
        job_id=job.job_id,
        request_fingerprint="0" * 64,
        target_draft_version=draft.manifest.draft_version,
        next_draft_version=next_version,
        revision_count=next_count,
        target_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        target_draft_artifact=draft.manifest.draft_artifact,
        target_draft_markdown=_draft_text(workspace, draft),
        author_claims=list(draft.claims),
        requirement_map=draft.requirement_map,
        audit_bundle_fingerprint=audit.manifest.bundle_fingerprint,
        audit_finding_artifact=audit.manifest.findings_artifact,
        audit_findings=list(audit.findings),
        fixed_evidence=fixed,
        output_constraints=_constraints(job, fixed),
        skill=skill,
        created_at=utc_now(),
    )
    request = provisional.model_copy(
        update={"request_fingerprint": _request_fingerprint(provisional)}
    )
    _write_machine_json(path, request, code="REVISION_REQUEST_INVALID")
    return RevisionPrepareResult(request=request, reused=False)


def load_revision_request(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    *,
    deep: bool = False,
) -> RevisionRequestManifest:
    _validate_revision_lineage(
        job,
        fixed,
        draft,
        audit,
        code="REVISION_REQUEST_STALE",
        deep=deep,
    )
    next_version = f"v{draft.manifest.revision_count + 2:03d}"
    request = _read_model(
        _revision_request_path(workspace, next_version),
        RevisionRequestManifest,
        code="REVISION_REQUEST_INVALID",
    )
    _validate_request_fingerprint(request, deep=deep)
    if not _revision_request_is_bound(workspace, request, job, fixed, draft, audit, deep=deep):
        raise integrity_error("REVISION_REQUEST_STALE", "revision request is stale")
    return request


def _validate_revision_response_semantics(
    response: RevisionResponseManifest,
    request: RevisionRequestManifest,
    *,
    forbidden_context_ids: tuple[str, ...],
    check_fingerprints: bool = True,
) -> None:
    if check_fingerprints and response.request_fingerprint != request.request_fingerprint:
        raise integrity_error(
            "REVISION_RESPONSE_INVALID", "revision response targets another request"
        )
    if response.reviser.context_id in forbidden_context_ids:
        raise integrity_error("REVISION_CONTEXT_NOT_FRESH", "reviser context is not fresh")
    _validate_claims(response.claims, response.requirement_map, request.fixed_evidence)
    finding_ids = [item.finding_id for item in request.audit_findings]
    if [item.finding_id for item in response.resolutions] != sorted(finding_ids):
        raise integrity_error(
            "REVISION_RESPONSE_INVALID", "every audit finding must be resolved exactly once"
        )
    known_evidence = {item.evidence_id for item in request.fixed_evidence.evidence_records}
    findings_by_id = {item.finding_id: item for item in request.audit_findings}
    declared_changed: list[tuple[int, int, str]] = []
    declared_removed: list[tuple[int, int, str]] = []
    declared_by_finding: dict[
        str, tuple[list[tuple[int, int, str]], list[tuple[int, int, str]]]
    ] = {}
    for resolution in response.resolutions:
        if not set(resolution.evidence_ids) <= known_evidence:
            raise integrity_error("REVISION_RESPONSE_INVALID", "resolution evidence is invalid")
        finding = findings_by_id[resolution.finding_id]
        if (
            resolution.decision is ResolutionDecision.ACCEPT
            and resolution.evidence_ids != finding.evidence_ids
        ):
            raise integrity_error(
                "REVISION_RESPONSE_INVALID",
                "accepted resolution evidence must exactly match its audit finding",
            )
        local_changed: list[tuple[int, int, str]] = []
        for span in resolution.changed_spans:
            if span.end > len(response.draft_markdown) or (
                response.draft_markdown[span.start : span.end] != span.text
            ):
                raise integrity_error("REVISION_RESPONSE_INVALID", "changed span is invalid")
            local_changed.append((span.start, span.end, span.text))
        local_removed: list[tuple[int, int, str]] = []
        for span in resolution.removed_spans:
            if span.end > len(request.target_draft_markdown) or (
                request.target_draft_markdown[span.start : span.end] != span.text
            ):
                raise integrity_error("REVISION_RESPONSE_INVALID", "removed span is invalid")
            local_removed.append((span.start, span.end, span.text))
        if local_changed != sorted(set(local_changed)) or local_removed != sorted(
            set(local_removed)
        ):
            raise integrity_error("REVISION_RESPONSE_INVALID", "resolution spans are not canonical")
        declared_by_finding[resolution.finding_id] = (local_changed, local_removed)
        declared_changed.extend(local_changed)
        declared_removed.extend(local_removed)
    expected_changed: list[tuple[int, int, str]] = []
    expected_removed: list[tuple[int, int, str]] = []
    changed_to_old_position: dict[tuple[int, int, str], tuple[int, int]] = {}
    removed_to_old_position: dict[tuple[int, int, str], tuple[int, int]] = {}
    matcher = SequenceMatcher(
        None,
        request.target_draft_markdown,
        response.draft_markdown,
        autojunk=False,
    )
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if old_start < old_end:
            removed = (
                old_start,
                old_end,
                request.target_draft_markdown[old_start:old_end],
            )
            expected_removed.append(removed)
            removed_to_old_position[removed] = (old_start, old_end)
        if new_start < new_end:
            changed = (
                new_start,
                new_end,
                response.draft_markdown[new_start:new_end],
            )
            expected_changed.append(changed)
            changed_to_old_position[changed] = (old_start, old_end)
    if set(declared_changed) != set(expected_changed) or set(declared_removed) != set(
        expected_removed
    ):
        raise integrity_error(
            "REVISION_RESPONSE_INVALID",
            "resolution ledger does not exactly cover the deterministic draft diff",
        )
    for finding_id, (changed, removed) in declared_by_finding.items():
        anchor = findings_by_id[finding_id].draft_span
        old_positions = [changed_to_old_position[hunk] for hunk in changed]
        old_positions.extend(removed_to_old_position[hunk] for hunk in removed)
        for old_start, old_end in old_positions:
            if anchor is None or not _hunk_touches_draft_anchor(anchor, old_start, old_end):
                raise integrity_error(
                    "REVISION_RESPONSE_INVALID",
                    "resolution diff hunk is not attributable to its audit finding anchor",
                )


def _hunk_touches_draft_anchor(anchor: DraftSpan, old_start: int, old_end: int) -> bool:
    """Match old-side hunks to a finding anchor, including zero-width inserts."""

    if old_start == old_end:
        return anchor.start <= old_start <= anchor.end
    return old_start < anchor.end and anchor.start < old_end


def read_revision_response(
    workspace: JobWorkspace,
    path: Path,
    request: RevisionRequestManifest,
    *,
    forbidden_context_ids: tuple[str, ...],
    allow_fixture: bool,
    enforce_current_skill: bool = True,
) -> RevisionResponseManifest:
    response = _read_model(path, RevisionResponseManifest, code="REVISION_RESPONSE_INVALID")
    _validate_execution(response.reviser, allow_fixture=allow_fixture)
    if enforce_current_skill:
        _validate_import_skill(
            workspace,
            request.skill,
            response.reviser,
            allowed=_REVISION_SKILLS,
            allow_fixture=allow_fixture,
        )
    _validate_revision_response_semantics(
        response,
        request,
        forbidden_context_ids=forbidden_context_ids,
    )
    return response


def stage_revision(
    workspace: JobWorkspace,
    job: JobSpec,
    request: RevisionRequestManifest,
    response: RevisionResponseManifest,
    draft: DraftResult,
    audit: AuditResult,
    history: AuthoringHistory,
    attempt: Path,
) -> RevisionResult:
    if not _valid_attempt(_staging_root(workspace), attempt):
        raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
    _validate_revision_lineage(
        job,
        request.fixed_evidence,
        draft,
        audit,
        code="REVISION_RESPONSE_INVALID",
    )
    if not history or history[-1] != (draft, audit):
        raise integrity_error("REVISION_RESPONSE_INVALID", "revision history is not current")
    authoring_context_ids, auditor_context_ids = _validated_history_contexts(
        history,
        job=job,
        fixed=request.fixed_evidence,
        latest_draft=draft,
    )
    prior_context_ids = tuple(sorted({*authoring_context_ids, *auditor_context_ids}))
    if not _revision_request_is_bound(
        workspace,
        request,
        job,
        request.fixed_evidence,
        draft,
        audit,
    ):
        raise integrity_error("REVISION_RESPONSE_INVALID", "revision inputs are not bound")
    _validate_revision_response_semantics(
        response,
        request,
        forbidden_context_ids=prior_context_ids,
    )
    _validate_execution(response.reviser, allow_fixture=True)
    draft_bytes = response.draft_markdown.encode("utf-8")
    if len(draft_bytes) > _MAXIMUM_DRAFT_BYTES:
        raise integrity_error("REVISION_RESPONSE_INVALID", "draft exceeds the UTF-8 byte limit")
    response_fingerprint = fingerprint(response)
    version = request.next_draft_version
    length_check, length_adjustment_count = _enforce_length_gate(
        workspace,
        job,
        version=version,
        draft_markdown=response.draft_markdown,
        response_fingerprint=response_fingerprint,
        response_payload=pretty_json_bytes(response),
        response_created_at=response.created_at,
        request_fingerprint=response.request_fingerprint,
    )
    draft_base = f"authoring/drafts/{version}"
    revision_base = f"authoring/revisions/{version}"
    draft_path, draft_artifact = _write_attempt_artifact(
        attempt,
        "draft.md",
        draft_bytes,
        artifact_type="draft_markdown",
        relative_path=f"{draft_base}/draft.md",
    )
    claims_path, claims_artifact = _write_attempt_artifact(
        attempt,
        "claims.jsonl",
        _jsonl_bytes(response.claims),
        artifact_type="claim_ledger",
        relative_path=f"{draft_base}/claims.jsonl",
    )
    map_path, map_artifact = _write_attempt_artifact(
        attempt,
        "requirement-map.json",
        pretty_json_bytes(response.requirement_map),
        artifact_type="requirement_map",
        relative_path=f"{draft_base}/requirement-map.json",
    )
    resolutions_path, resolutions_artifact = _write_attempt_artifact(
        attempt,
        "resolutions.jsonl",
        _jsonl_bytes(response.resolutions),
        artifact_type="resolution_ledger",
        relative_path=f"{revision_base}/resolutions.jsonl",
    )
    response_path, response_artifact = _write_attempt_artifact(
        attempt,
        "validated-response.json",
        pretty_json_bytes(response),
        artifact_type="validated_revision_response",
        relative_path=_response_artifact_path(response_fingerprint, "revision"),
    )
    started = utc_now()
    run = AgentRunRecord(
        role=AgentRole.REVISER,
        skill=request.skill,
        identity=response.reviser,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        input_artifacts=[
            draft.manifest.draft_artifact,
            audit.manifest.findings_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
        ],
        output_artifacts=[
            draft_artifact,
            claims_artifact,
            map_artifact,
            resolutions_artifact,
            response_artifact,
        ],
        started_at=started,
        finished_at=utc_now(),
    )
    run_path, run_artifact = _write_attempt_artifact(
        attempt,
        "agent-run.json",
        pretty_json_bytes(run),
        artifact_type="agent_run",
        relative_path=f"{draft_base}/agent-run.json",
    )
    provisional_draft = DraftManifest(
        job_id=job.job_id,
        draft_version=version,
        revision_count=request.revision_count,
        semantic_revision_count=request.revision_count,
        length_adjustment_count=length_adjustment_count,
        length_check=length_check,
        predecessor_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        fixed_evidence_fingerprint=request.fixed_evidence.fixed_evidence_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        bundle_fingerprint="0" * 64,
        producer=response.reviser,
        draft_artifact=draft_artifact,
        claims_artifact=claims_artifact,
        requirement_map_artifact=map_artifact,
        agent_run_artifact=run_artifact,
        resolution_artifact=resolutions_artifact,
        created_at=run.finished_at,
    )
    draft_manifest = provisional_draft.model_copy(
        update={"bundle_fingerprint": _draft_fingerprint(provisional_draft)}
    )
    provisional_revision = RevisionManifest(
        job_id=job.job_id,
        target_draft_version=draft.manifest.draft_version,
        revised_draft_version=version,
        target_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        audit_bundle_fingerprint=audit.manifest.bundle_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        resolution_artifact=resolutions_artifact,
        prior_context_ids=list(prior_context_ids),
        bundle_fingerprint="0" * 64,
        created_at=run.finished_at,
    )
    revision_manifest = provisional_revision.model_copy(
        update={"bundle_fingerprint": _revision_fingerprint(provisional_revision)}
    )
    stage = StageManifest(
        job_id=job.job_id,
        stage=f"revision-{version}",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.DRAFTED,
        input_artifacts=[
            draft.manifest.draft_artifact,
            audit.manifest.findings_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
        ],
        output_artifacts=[
            draft_artifact,
            claims_artifact,
            map_artifact,
            resolutions_artifact,
            response_artifact,
            run_artifact,
        ],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-revision-import",
            version=__version__,
        ),
        configuration_sha256=draft_manifest.bundle_fingerprint,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    staged_draft = DraftResult(
        manifest=draft_manifest,
        stage_manifest=stage,
        response=response,
        claims=tuple(response.claims),
        requirement_map=response.requirement_map,
        agent_run=run,
        reused=False,
    )
    for path, artifact in (
        (draft_path, draft_artifact),
        (claims_path, claims_artifact),
        (map_path, map_artifact),
        (resolutions_path, resolutions_artifact),
        (response_path, response_artifact),
        (run_path, run_artifact),
    ):
        _verify_artifact(path, artifact)
    return RevisionResult(
        draft=staged_draft,
        manifest=revision_manifest,
        resolutions=tuple(response.resolutions),
        reused=False,
    )


def recover_revision(
    workspace: JobWorkspace,
    job: JobSpec,
    request: RevisionRequestManifest,
    response: RevisionResponseManifest,
    predecessor: DraftResult,
    audit: AuditResult,
    history: AuthoringHistory,
) -> RevisionResult | None:
    """Adopt a complete revision run when its draft commit marker is absent."""

    _validate_revision_lineage(
        job,
        request.fixed_evidence,
        predecessor,
        audit,
        code="REVISION_ORPHAN_INVALID",
    )
    if not history or history[-1] != (predecessor, audit):
        raise integrity_error("REVISION_ORPHAN_INVALID", "orphan revision history is not current")
    version = request.next_draft_version
    draft_base = f"authoring/drafts/{version}"
    revision_base = f"authoring/revisions/{version}"
    run_relative = f"{draft_base}/agent-run.json"
    if not workspace.safe_path(run_relative).exists():
        return None
    authoring_context_ids, auditor_context_ids = _validated_history_contexts(
        history,
        job=job,
        fixed=request.fixed_evidence,
        latest_draft=predecessor,
    )
    prior_context_ids = tuple(sorted({*authoring_context_ids, *auditor_context_ids}))
    _validate_revision_response_semantics(
        response,
        request,
        forbidden_context_ids=prior_context_ids,
    )
    response_fingerprint = fingerprint(response)
    draft_artifact = _existing_artifact(
        workspace,
        f"{draft_base}/draft.md",
        artifact_type="draft_markdown",
        code="REVISION_ORPHAN_INVALID",
    )
    claims_artifact = _existing_artifact(
        workspace,
        f"{draft_base}/claims.jsonl",
        artifact_type="claim_ledger",
        code="REVISION_ORPHAN_INVALID",
    )
    map_artifact = _existing_artifact(
        workspace,
        f"{draft_base}/requirement-map.json",
        artifact_type="requirement_map",
        code="REVISION_ORPHAN_INVALID",
    )
    resolutions_artifact = _existing_artifact(
        workspace,
        f"{revision_base}/resolutions.jsonl",
        artifact_type="resolution_ledger",
        code="REVISION_ORPHAN_INVALID",
    )
    response_artifact = _existing_artifact(
        workspace,
        _response_artifact_path(response_fingerprint, "revision"),
        artifact_type="validated_revision_response",
        code="REVISION_ORPHAN_INVALID",
    )
    run_artifact = _existing_artifact(
        workspace,
        run_relative,
        artifact_type="agent_run",
        code="REVISION_ORPHAN_INVALID",
    )
    try:
        stored_draft = _read_bounded_bytes(
            workspace.safe_path(draft_artifact.relative_storage_path),
            maximum_bytes=_MAXIMUM_DRAFT_BYTES,
            code="REVISION_ORPHAN_INVALID",
        ).decode("utf-8")
    except UnicodeError:
        raise integrity_error(
            "REVISION_ORPHAN_INVALID", "orphan revision draft is not valid UTF-8"
        ) from None
    stored_response = _read_model(
        workspace.safe_path(response_artifact.relative_storage_path),
        RevisionResponseManifest,
        code="REVISION_ORPHAN_INVALID",
    )
    stored_claims = _read_jsonl(
        workspace.safe_path(claims_artifact.relative_storage_path),
        ClaimRecord,
        code="REVISION_ORPHAN_INVALID",
    )
    stored_map = _read_model(
        workspace.safe_path(map_artifact.relative_storage_path),
        RequirementMap,
        code="REVISION_ORPHAN_INVALID",
    )
    stored_resolutions = _read_jsonl(
        workspace.safe_path(resolutions_artifact.relative_storage_path),
        ResolutionRecord,
        code="REVISION_ORPHAN_INVALID",
    )
    run = _read_model(
        workspace.safe_path(run_artifact.relative_storage_path),
        AgentRunRecord,
        code="REVISION_ORPHAN_INVALID",
    )
    expected_inputs = [
        ArtifactRef.model_validate(artifact.model_dump())
        for artifact in (
            predecessor.manifest.draft_artifact,
            audit.manifest.findings_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
        )
    ]
    expected_outputs = [
        draft_artifact,
        claims_artifact,
        map_artifact,
        resolutions_artifact,
        response_artifact,
    ]
    if (
        stored_response != response
        or stored_draft != response.draft_markdown
        or stored_claims != tuple(response.claims)
        or stored_map != response.requirement_map
        or stored_resolutions != tuple(response.resolutions)
        or run.role is not AgentRole.REVISER
        or run.skill != request.skill
        or run.identity != response.reviser
        or run.request_fingerprint != request.request_fingerprint
        or run.response_fingerprint != response_fingerprint
        or run.input_artifacts != expected_inputs
        or run.output_artifacts != expected_outputs
    ):
        raise integrity_error("REVISION_ORPHAN_INVALID", "orphan revision content is inconsistent")
    length_check, length_adjustment_count = _recovered_length_state(
        workspace,
        job,
        version=version,
        draft_markdown=response.draft_markdown,
    )
    provisional_draft = DraftManifest(
        job_id=job.job_id,
        draft_version=version,
        revision_count=request.revision_count,
        semantic_revision_count=request.revision_count,
        length_adjustment_count=length_adjustment_count,
        length_check=length_check,
        predecessor_draft_sha256=predecessor.manifest.draft_artifact.content_sha256,
        fixed_evidence_fingerprint=request.fixed_evidence.fixed_evidence_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        bundle_fingerprint="0" * 64,
        producer=response.reviser,
        draft_artifact=draft_artifact,
        claims_artifact=claims_artifact,
        requirement_map_artifact=map_artifact,
        agent_run_artifact=run_artifact,
        resolution_artifact=resolutions_artifact,
        created_at=run.finished_at,
    )
    draft_manifest = provisional_draft.model_copy(
        update={"bundle_fingerprint": _draft_fingerprint(provisional_draft)}
    )
    provisional_revision = RevisionManifest(
        job_id=job.job_id,
        target_draft_version=predecessor.manifest.draft_version,
        revised_draft_version=version,
        target_draft_sha256=predecessor.manifest.draft_artifact.content_sha256,
        audit_bundle_fingerprint=audit.manifest.bundle_fingerprint,
        request_fingerprint=request.request_fingerprint,
        response_fingerprint=response_fingerprint,
        resolution_artifact=resolutions_artifact,
        prior_context_ids=list(prior_context_ids),
        bundle_fingerprint="0" * 64,
        created_at=run.finished_at,
    )
    revision_manifest = provisional_revision.model_copy(
        update={"bundle_fingerprint": _revision_fingerprint(provisional_revision)}
    )
    stage = StageManifest(
        job_id=job.job_id,
        stage=f"revision-{version}",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.DRAFTED,
        input_artifacts=[
            predecessor.manifest.draft_artifact,
            audit.manifest.findings_artifact,
            request.fixed_evidence.evidence_manifest.evidence_artifact,
        ],
        output_artifacts=[*run.output_artifacts, run_artifact],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-revision-import",
            version=__version__,
        ),
        configuration_sha256=draft_manifest.bundle_fingerprint,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    return RevisionResult(
        draft=DraftResult(
            manifest=draft_manifest,
            stage_manifest=stage,
            response=response,
            claims=stored_claims,
            requirement_map=stored_map,
            agent_run=run,
            reused=False,
        ),
        manifest=revision_manifest,
        resolutions=stored_resolutions,
        reused=False,
    )


def commit_revision(
    workspace: JobWorkspace,
    staged: RevisionResult,
    *,
    attempt: Path | None,
) -> RevisionResult:
    draft = staged.draft
    if draft.manifest.resolution_artifact is None:
        raise integrity_error("REVISION_BUNDLE_INVALID", "revision resolution artifact is absent")
    response_artifact = next(
        item
        for item in draft.stage_manifest.output_artifacts
        if item.artifact_type == "validated_revision_response"
    )
    resolved_artifacts = (
        ("draft.md", draft.manifest.draft_artifact),
        ("claims.jsonl", draft.manifest.claims_artifact),
        ("requirement-map.json", draft.manifest.requirement_map_artifact),
        ("resolutions.jsonl", draft.manifest.resolution_artifact),
        ("validated-response.json", response_artifact),
        ("agent-run.json", draft.manifest.agent_run_artifact),
    )
    if attempt is not None:
        if not _valid_attempt(_staging_root(workspace), attempt):
            raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
        for filename, artifact in resolved_artifacts:
            _publish(workspace, attempt / filename, artifact)
    else:
        for _filename, artifact in resolved_artifacts:
            _verify_artifact(workspace.safe_path(artifact.relative_storage_path), artifact)
    version = draft.manifest.draft_version
    _write_or_verify_model(
        _draft_stage_path(workspace, version),
        draft.stage_manifest,
        StageManifest,
        code="STAGE_MANIFEST_MISMATCH",
    )
    _write_or_verify_model(
        _revision_manifest_path(workspace, version),
        staged.manifest,
        RevisionManifest,
        code="REVISION_MANIFEST_MISMATCH",
    )
    _write_or_verify_model(
        _draft_manifest_path(workspace, version),
        draft.manifest,
        DraftManifest,
        code="DRAFT_MANIFEST_MISMATCH",
    )
    return staged


def load_revision(
    workspace: JobWorkspace,
    job: JobSpec,
    version: str,
    fixed: FixedEvidenceBundle,
    predecessor: DraftResult,
    audit: AuditResult,
    request: RevisionRequestManifest,
    history: AuthoringHistory,
    *,
    deep: bool = False,
) -> RevisionResult:
    _validate_revision_lineage(
        job,
        fixed,
        predecessor,
        audit,
        code="REVISION_BUNDLE_INVALID",
        deep=deep,
    )
    if not history or history[-1] != (predecessor, audit):
        raise integrity_error("REVISION_BUNDLE_INVALID", "revision history is not current")
    authoring_context_ids, auditor_context_ids = _validated_history_contexts(
        history,
        job=job,
        fixed=fixed,
        latest_draft=predecessor,
        deep=deep,
    )
    prior_context_ids = tuple(sorted({*authoring_context_ids, *auditor_context_ids}))
    _validate_request_fingerprint(request, deep=deep)
    if not _revision_request_is_bound(
        workspace, request, job, fixed, predecessor, audit, deep=deep
    ):
        raise integrity_error("REVISION_BUNDLE_INVALID", "revision request binding is invalid")
    draft = load_draft(workspace, job, version, fixed, deep=deep)
    if draft.manifest.revision_count == 0 or draft.manifest.resolution_artifact is None:
        raise integrity_error("REVISION_BUNDLE_INVALID", "draft is not a revision")
    manifest = _read_model(
        _revision_manifest_path(workspace, version),
        RevisionManifest,
        code="REVISION_MANIFEST_INVALID",
    )
    if (
        manifest.job_id != job.job_id
        or manifest.target_draft_version != predecessor.manifest.draft_version
        or manifest.revised_draft_version != version
        or manifest.target_draft_sha256 != predecessor.manifest.draft_artifact.content_sha256
        or manifest.resolution_artifact != draft.manifest.resolution_artifact
        or manifest.prior_context_ids != list(prior_context_ids)
        or draft.manifest.predecessor_draft_sha256
        != predecessor.manifest.draft_artifact.content_sha256
        or (
            deep
            and (
                manifest.audit_bundle_fingerprint != audit.manifest.bundle_fingerprint
                or manifest.request_fingerprint != request.request_fingerprint
                or manifest.response_fingerprint != draft.manifest.response_fingerprint
                or draft.manifest.request_fingerprint != request.request_fingerprint
                or manifest.bundle_fingerprint != _revision_fingerprint(manifest)
            )
        )
    ):
        raise integrity_error("REVISION_BUNDLE_INVALID", "revision manifest binding is invalid")
    resolution_path = workspace.safe_path(manifest.resolution_artifact.relative_storage_path)
    _verify_artifact(resolution_path, manifest.resolution_artifact, deep=deep)
    resolutions = _read_jsonl(
        resolution_path,
        ResolutionRecord,
        code="REVISION_BUNDLE_INVALID",
    )
    response = draft.response
    if (
        not isinstance(response, RevisionResponseManifest)
        or tuple(response.resolutions) != resolutions
    ):
        raise integrity_error("REVISION_BUNDLE_INVALID", "revision resolution ledger is invalid")
    _validate_execution(response.reviser, allow_fixture=True)
    _validate_revision_response_semantics(
        response,
        request,
        forbidden_context_ids=prior_context_ids,
        check_fingerprints=deep,
    )
    return RevisionResult(draft=draft, manifest=manifest, resolutions=resolutions, reused=True)


def _human_review_stage_path(workspace: JobWorkspace, version: str) -> Path:
    return workspace.safe_path(f"manifests/stages/human-review-{version}.json")


def _human_review_manifest_fingerprint(manifest: HumanReviewManifest) -> str:
    return fingerprint(manifest.model_dump(mode="json", exclude={"bundle_fingerprint"}))


_HUMAN_REVIEW_PR46_FIELDS = frozenset(
    {
        "length_check",
        "semantic_revision_count",
        "length_adjustment_count",
    }
)


def canonical_human_review_bundle_payload(bundle: HumanReviewBundle) -> dict[str, object]:
    """Select the exact historical HumanReviewBundle artifact shape."""

    present = _HUMAN_REVIEW_PR46_FIELDS & bundle.model_fields_set
    if present and present != _HUMAN_REVIEW_PR46_FIELDS:
        raise integrity_error(
            "HUMAN_REVIEW_BUNDLE_INVALID",
            "human review historical field presence is ambiguous",
        )
    payload = bundle.model_dump(mode="json")
    if not present:
        for field in _HUMAN_REVIEW_PR46_FIELDS:
            payload.pop(field)
    return payload


def canonical_human_review_bundle_bytes(bundle: HumanReviewBundle) -> bytes:
    """Serialize a current or pre-PR46 bundle without changing global JSON policy."""

    return pretty_json_bytes(canonical_human_review_bundle_payload(bundle))


def _human_review_bundle_matches(
    stored: HumanReviewBundle,
    expected: HumanReviewBundle,
    *,
    deep: bool = False,
) -> bool:
    if deep:
        return stored == expected
    stored_values = stored.model_dump(mode="json")
    expected_values = expected.model_dump(mode="json")
    for values in (stored_values, expected_values):
        for key in (
            "fixed_evidence_fingerprint",
            "audit_bundle_fingerprint",
            "current_draft_sha256",
            "approval_target_sha256",
        ):
            values.pop(key, None)
        for entry in values["revision_history"]:
            entry.pop("draft_sha256", None)
            entry.pop("audit_bundle_fingerprint", None)
    return stored_values == expected_values


def _build_human_review_bundle(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    history: AuthoringHistory,
    *,
    created_at: datetime,
) -> HumanReviewBundle:
    _validated_history_contexts(
        history,
        job=job,
        fixed=fixed,
        latest_draft=draft,
    )
    if (
        history[-1] != (draft, audit)
        or audit.manifest.disposition is not AuditDisposition.HUMAN_REVIEW_READY
        or audit.manifest.p0_count != 0
        or audit.manifest.p1_count != 0
    ):
        raise integrity_error(
            "HUMAN_REVIEW_BUNDLE_INVALID",
            "human review inputs are not the latest clean authoring history",
        )
    if any(
        item.coverage is not RequirementCoverage.SATISFIED for item in draft.requirement_map.entries
    ):
        raise state_error("HUMAN_REVIEW_GATE_FAILED", "assignment requirements are incomplete")
    draft_text = _draft_text(workspace, draft)
    if not draft_text.strip():
        raise state_error("HUMAN_REVIEW_GATE_FAILED", "draft Markdown is empty")
    length_check = validate_character_range(
        draft_text,
        resolve_length_constraint(job.assignment.length),
    )
    if length_check is not None and not length_check.passed:
        raise state_error(
            "HUMAN_REVIEW_LENGTH_GATE_FAILED",
            "current draft does not satisfy the effective character range",
        )
    evidence_by_id = {item.evidence_id: item for item in fixed.evidence_records}
    replay: dict[tuple[str, str, int | None, int | None, str], SourceRef] = {}
    for claim in draft.claims:
        for evidence_id in claim.evidence_ids:
            for source in evidence_by_id[evidence_id].source_refs:
                replay[_source_ref_key(source)] = source
    history_entries = [
        RevisionHistoryEntry(
            draft_version=historical_draft.manifest.draft_version,
            draft_sha256=historical_draft.manifest.draft_artifact.content_sha256,
            audit_bundle_fingerprint=(
                historical_audit.manifest.bundle_fingerprint
                if historical_audit is not None
                else None
            ),
            resolution_artifact=historical_draft.manifest.resolution_artifact,
        )
        for historical_draft, historical_audit in history
    ]
    return HumanReviewBundle(
        job_id=job.job_id,
        fixed_evidence_fingerprint=fixed.fixed_evidence_fingerprint,
        current_draft_version=draft.manifest.draft_version,
        current_draft_artifact=draft.manifest.draft_artifact,
        current_draft_sha256=draft.manifest.draft_artifact.content_sha256,
        audit_bundle_fingerprint=audit.manifest.bundle_fingerprint,
        audit_findings_artifact=audit.manifest.findings_artifact,
        claim_comparison_artifact=audit.manifest.claim_comparison_artifact,
        p0_count=0,
        p1_count=0,
        p2_count=audit.manifest.p2_count,
        unresolved_ambiguity_ids=sorted(
            item.ambiguity_id
            for item in fixed.ambiguity_records
            if item.status is AmbiguityStatus.UNRESOLVED
        ),
        confirmation_spans=[
            item.draft_span for item in draft.claims if item.requires_user_confirmation
        ],
        user_opinion_claim_ids=sorted(
            item.claim_id for item in draft.claims if item.claim_type is ClaimType.USER_OPINION
        ),
        external_fact_claim_ids=sorted(
            item.claim_id for item in draft.claims if item.claim_type is ClaimType.EXTERNAL_FACT
        ),
        replay_source_refs=[replay[key] for key in sorted(replay)],
        requirement_map=draft.requirement_map,
        format_checks=[
            FormatCheck(
                check_id="markdown.nonempty",
                passed=bool(draft_text.strip()),
                detail="draft Markdown is non-empty UTF-8",
            ),
            FormatCheck(
                check_id="requirements.complete",
                passed=True,
                detail="all ordered assignment requirements are satisfied",
            ),
            FormatCheck(
                check_id="audit.material-findings",
                passed=True,
                detail="latest independent audit has P0=0 and P1=0",
            ),
            FormatCheck(
                check_id="audit.comparison-available",
                passed=True,
                detail="deterministic author-versus-audit comparison is attached",
            ),
            *(
                [
                    FormatCheck(
                        check_id="length.character-range",
                        passed=True,
                        detail=(
                            f"deterministic character count {length_check.actual_chars} is within "
                            f"{length_check.effective.min_chars}-"
                            f"{length_check.effective.max_chars}"
                        ),
                    )
                ]
                if length_check is not None
                else []
            ),
        ],
        length_check=length_check,
        semantic_revision_count=draft.manifest.semantic_revision_count,
        length_adjustment_count=(
            draft.manifest.length_adjustment_count
            if draft.manifest.semantic_revision_count is not None
            else None
        ),
        revision_history=history_entries,
        approval_target_sha256=draft.manifest.draft_artifact.content_sha256,
        is_approval=False,
        created_at=created_at,
    )


def _human_review_result_from_bundle(
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    bundle: HumanReviewBundle,
    artifact: ArtifactRef,
    *,
    reused: bool,
) -> HumanReviewResult:
    """Build deterministic manifests from the durable human-review bundle."""

    provisional = HumanReviewManifest(
        job_id=job.job_id,
        fixed_evidence_fingerprint=fixed.fixed_evidence_fingerprint,
        draft_bundle_fingerprint=draft.manifest.bundle_fingerprint,
        audit_bundle_fingerprint=audit.manifest.bundle_fingerprint,
        bundle_fingerprint="0" * 64,
        bundle_artifact=artifact,
        approval_target_sha256=draft.manifest.draft_artifact.content_sha256,
        created_at=bundle.created_at,
    )
    manifest = provisional.model_copy(
        update={"bundle_fingerprint": _human_review_manifest_fingerprint(provisional)}
    )
    stage = StageManifest(
        job_id=job.job_id,
        stage=f"human-review-{draft.manifest.draft_version}",
        status=StageStatus.SUCCEEDED,
        state_transition=JobState.HUMAN_REVIEW_READY,
        input_artifacts=[
            draft.manifest.draft_artifact,
            audit.manifest.findings_artifact,
            audit.manifest.claim_comparison_artifact,
            fixed.evidence_manifest.evidence_artifact,
        ],
        output_artifacts=[artifact],
        producer=ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-human-review",
            version=__version__,
        ),
        configuration_sha256=manifest.bundle_fingerprint,
        started_at=bundle.created_at,
        finished_at=bundle.created_at,
    )
    return HumanReviewResult(
        manifest=manifest,
        bundle=bundle,
        stage_manifest=stage,
        reused=reused,
    )


def stage_human_review(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    history: tuple[tuple[DraftResult, AuditResult | None], ...],
    attempt: Path,
) -> HumanReviewResult:
    if not _valid_attempt(_staging_root(workspace), attempt):
        raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
    bundle = _build_human_review_bundle(
        workspace,
        job,
        fixed,
        draft,
        audit,
        history,
        created_at=utc_now(),
    )
    path, artifact = _write_attempt_artifact(
        attempt,
        "human-review.json",
        canonical_human_review_bundle_bytes(bundle),
        artifact_type="human_review_bundle",
        relative_path=f"human-review/{draft.manifest.draft_version}/bundle.json",
    )
    _verify_artifact(path, artifact)
    return _human_review_result_from_bundle(
        job,
        fixed,
        draft,
        audit,
        bundle,
        artifact,
        reused=False,
    )


def recover_human_review(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    history: AuthoringHistory,
) -> HumanReviewResult | None:
    """Adopt a published review bundle when its stage and primary manifest are absent."""

    version = draft.manifest.draft_version
    relative_path = f"human-review/{version}/bundle.json"
    if not workspace.safe_path(relative_path).exists():
        return None
    artifact = _existing_artifact(
        workspace,
        relative_path,
        artifact_type="human_review_bundle",
        code="HUMAN_REVIEW_ORPHAN_INVALID",
    )
    bundle = _read_model(
        workspace.safe_path(relative_path),
        HumanReviewBundle,
        code="HUMAN_REVIEW_ORPHAN_INVALID",
    )
    expected_bundle = _build_human_review_bundle(
        workspace,
        job,
        fixed,
        draft,
        audit,
        history,
        created_at=bundle.created_at,
    )
    if not _human_review_bundle_matches(bundle, expected_bundle):
        raise integrity_error(
            "HUMAN_REVIEW_ORPHAN_INVALID",
            "orphan human-review bundle is inconsistent",
        )
    return _human_review_result_from_bundle(
        job,
        fixed,
        draft,
        audit,
        bundle,
        artifact,
        reused=False,
    )


def commit_human_review(
    workspace: JobWorkspace,
    staged: HumanReviewResult,
    *,
    attempt: Path | None,
) -> HumanReviewResult:
    if attempt is not None:
        if not _valid_attempt(_staging_root(workspace), attempt):
            raise integrity_error("AUTHORING_STAGING_INVALID", "semantic staging is invalid")
        _publish(workspace, attempt / "human-review.json", staged.manifest.bundle_artifact)
    else:
        _verify_artifact(
            workspace.safe_path(staged.manifest.bundle_artifact.relative_storage_path),
            staged.manifest.bundle_artifact,
        )
    version = staged.bundle.current_draft_version
    _write_or_verify_model(
        _human_review_stage_path(workspace, version),
        staged.stage_manifest,
        StageManifest,
        code="STAGE_MANIFEST_MISMATCH",
    )
    _write_or_verify_model(
        workspace.human_review_manifest_json,
        staged.manifest,
        HumanReviewManifest,
        code="HUMAN_REVIEW_MANIFEST_MISMATCH",
    )
    return staged


def load_human_review(
    workspace: JobWorkspace,
    job: JobSpec,
    fixed: FixedEvidenceBundle,
    draft: DraftResult,
    audit: AuditResult,
    history: AuthoringHistory,
    *,
    deep: bool = False,
) -> HumanReviewResult:
    manifest = _read_model(
        workspace.human_review_manifest_json,
        HumanReviewManifest,
        code="HUMAN_REVIEW_MANIFEST_INVALID",
    )
    stage = _read_model(
        _human_review_stage_path(workspace, draft.manifest.draft_version),
        StageManifest,
        code="STAGE_MANIFEST_INVALID",
    )
    _verify_artifact(
        workspace.safe_path(manifest.bundle_artifact.relative_storage_path),
        manifest.bundle_artifact,
        deep=deep,
    )
    bundle = _read_model(
        workspace.safe_path(manifest.bundle_artifact.relative_storage_path),
        HumanReviewBundle,
        code="HUMAN_REVIEW_BUNDLE_INVALID",
    )
    expected_bundle = _build_human_review_bundle(
        workspace,
        job,
        fixed,
        draft,
        audit,
        history,
        created_at=bundle.created_at,
    )
    if (
        manifest.job_id != job.job_id
        or manifest.approval_target_sha256 != draft.manifest.draft_artifact.content_sha256
        or not _human_review_bundle_matches(bundle, expected_bundle, deep=deep)
        or bundle.current_draft_sha256 != draft.manifest.draft_artifact.content_sha256
        or bundle.approval_target_sha256 != manifest.approval_target_sha256
        or stage.job_id != job.job_id
        or stage.stage != f"human-review-{draft.manifest.draft_version}"
        or stage.status is not StageStatus.SUCCEEDED
        or stage.state_transition is not JobState.HUMAN_REVIEW_READY
        or (deep and stage.configuration_sha256 != manifest.bundle_fingerprint)
        or _artifact_payloads(stage.input_artifacts)
        != _artifact_payloads(
            [
                draft.manifest.draft_artifact,
                audit.manifest.findings_artifact,
                audit.manifest.claim_comparison_artifact,
                fixed.evidence_manifest.evidence_artifact,
            ]
        )
        or _artifact_payloads(stage.output_artifacts)
        != _artifact_payloads([manifest.bundle_artifact])
        or stage.producer
        != ProducerIdentity(
            kind=ProducerKind.TOOL,
            name="video-evidence-pipeline-human-review",
            version=__version__,
        )
        or stage.started_at != bundle.created_at
        or stage.finished_at != manifest.created_at
        or (
            deep
            and (
                not _fixed_evidence_fingerprint_matches(
                    fixed, manifest.fixed_evidence_fingerprint, deep=True
                )
                or manifest.draft_bundle_fingerprint != draft.manifest.bundle_fingerprint
                or manifest.audit_bundle_fingerprint != audit.manifest.bundle_fingerprint
                or manifest.bundle_fingerprint != _human_review_manifest_fingerprint(manifest)
                or sha256_bytes(canonical_human_review_bundle_bytes(bundle))
                != manifest.bundle_artifact.content_sha256
            )
        )
    ):
        raise integrity_error("HUMAN_REVIEW_BUNDLE_INVALID", "human review binding is invalid")
    return HumanReviewResult(
        manifest=manifest,
        bundle=bundle,
        stage_manifest=stage,
        reused=True,
    )
