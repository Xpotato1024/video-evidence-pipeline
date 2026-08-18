from __future__ import annotations

import json
import shutil
from pathlib import Path

from test_authoring_pipeline import _author_response, _write_model
from test_issue33_recovery import _block_initial_draft, _file_bytes
from test_transcription_evidence import (
    _response as _evidence_response,
)
from test_transcription_evidence import (
    _synthetic_captioned_source,
)
from test_transcription_evidence import (
    _write_response as _write_evidence_response,
)

from video_evidence_pipeline.acquisition.bundle import verify_acquired_bundle
from video_evidence_pipeline.application import (
    _import_authoring_fixture,
    _import_evidence_fixture,
    acquire_job,
    authorize_job,
    initialize_job,
    normalize_job,
    prepare_authoring_job,
    prepare_evidence_job,
    recover_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.authoring.pipeline import (
    _request_fingerprint,
    _request_fingerprint_matches,
    _validate_request_fingerprint,
)
from video_evidence_pipeline.domain.authoring import (
    AuditRequestManifest,
    AuthoringRequestManifest,
    RevisionRequestManifest,
    fixed_evidence_identity_matches,
)
from video_evidence_pipeline.domain.evidence import (
    ATOMIC_PROPOSITION_RULE,
    AnalyzerContract,
    AnalyzerExecutionMode,
    EvidenceManifest,
    EvidenceRequestManifest,
    EvidenceResponseManifest,
)
from video_evidence_pipeline.domain.job import (
    PrimaryInputKind,
    SourceKind,
    load_job_yaml,
    requires_transcript_refinement,
)
from video_evidence_pipeline.domain.media import MediaManifest
from video_evidence_pipeline.domain.serialization import fingerprint, pretty_json_bytes
from video_evidence_pipeline.domain.state import JobState
from video_evidence_pipeline.domain.transcription import TranscriptManifest
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.evidence.pipeline import load_evidence_result
from video_evidence_pipeline.media.normalization import load_normalization_result
from video_evidence_pipeline.transcription.pipeline import (
    load_transcription_result,
    media_bundle_fingerprint,
)


def _video_evidence_parent(tmp_path: Path, job_id: str):
    repository = tmp_path / f"{job_id}-repository"
    repository.mkdir()
    shutil.copytree(Path(__file__).parents[1] / ".agents", repository / ".agents")
    source = _synthetic_captioned_source(repository)
    workspace, _job = initialize_job(
        repository,
        job_id=job_id,
        source_kind=SourceKind.LOCAL_FILE,
        source=str(source),
        task_type="report",
        language="en",
        output_format="markdown",
        requirements=["Preserve only synthetic evidence."],
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="authorized synthetic legacy v1 fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="pytest",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)
    transcribe_job(workspace.root)
    request = prepare_evidence_job(workspace.root).request
    response_path = tmp_path / f"{job_id}-evidence-response.json"
    _write_evidence_response(
        response_path,
        _evidence_response(request, execution_mode=AnalyzerExecutionMode.TEST_FIXTURE),
    )
    _import_evidence_fixture(workspace.root, response_path)
    return workspace


def _pre_35_bundle_fingerprint(
    transcript: TranscriptManifest,
    media: MediaManifest,
    request: EvidenceRequestManifest,
    response: EvidenceResponseManifest,
) -> str:
    """Frozen pre-#35 payload: the optional analyzer field did not exist."""

    return fingerprint(
        {
            "transcript_bundle_fingerprint": transcript.transcription_fingerprint,
            "media_bundle_fingerprint": media_bundle_fingerprint(media),
            "frame_index_artifact": (
                media.frame_index_artifact.model_dump(mode="json")
                if media.frame_index_artifact is not None
                else None
            ),
            "request_fingerprint": request.request_fingerprint,
            "analyzer_contract": {
                "name": request.analyzer_contract.name,
                "version": request.analyzer_contract.version,
                "response_schema_version": request.analyzer_contract.response_schema_version,
            },
            "response": response.model_dump(mode="json"),
            "implementation_version": "evidence-v1",
        }
    )


def _pre_35_fixed_evidence_fingerprint(
    job_id: str,
    request: EvidenceRequestManifest,
    manifest: EvidenceManifest,
    evidence_records,
    ambiguity_records,
) -> str:
    request_payload = request.model_dump(mode="json")
    request_payload["analyzer_contract"] = request.analyzer_contract.model_dump(
        mode="json", exclude_none=True
    )
    return fingerprint(
        {
            "job_id": job_id,
            "evidence_request": request_payload,
            "evidence_manifest": manifest.model_dump(mode="json"),
            "evidence_records": [item.model_dump(mode="json") for item in evidence_records],
            "ambiguity_records": [item.model_dump(mode="json") for item in ambiguity_records],
        }
    )


def _freeze_pre_35_evidence(
    workspace,
) -> tuple[
    EvidenceManifest,
    EvidenceRequestManifest,
    MediaManifest,
    TranscriptManifest,
    EvidenceResponseManifest,
]:
    request_payload = json.loads(workspace.evidence_request_json.read_text(encoding="utf-8"))
    request_payload["analyzer_contract"].pop("atomic_proposition_contract", None)
    workspace.evidence_request_json.write_bytes(pretty_json_bytes(request_payload))

    request = EvidenceRequestManifest.model_validate(request_payload)
    manifest_payload = json.loads(workspace.evidence_manifest_json.read_text(encoding="utf-8"))
    manifest = EvidenceManifest.model_validate(manifest_payload)
    media = MediaManifest.model_validate_json(workspace.media_manifest_json.read_bytes())
    transcript = TranscriptManifest.model_validate_json(
        workspace.transcript_manifest_json.read_bytes()
    )
    response_path = workspace.safe_path(manifest.response_artifact.relative_storage_path)
    response = EvidenceResponseManifest.model_validate_json(response_path.read_bytes())
    legacy_fingerprint = _pre_35_bundle_fingerprint(transcript, media, request, response)

    manifest_payload["evidence_bundle_fingerprint"] = legacy_fingerprint
    workspace.evidence_manifest_json.write_bytes(pretty_json_bytes(manifest_payload))
    stage_path = workspace.evidence_stage_manifest_json
    stage_payload = json.loads(stage_path.read_text(encoding="utf-8"))
    stage_payload["configuration_sha256"] = legacy_fingerprint
    stage_path.write_bytes(pretty_json_bytes(stage_payload))
    return (
        EvidenceManifest.model_validate_json(workspace.evidence_manifest_json.read_bytes()),
        request,
        media,
        transcript,
        response,
    )


def _legacy_blocked_video_parent(tmp_path: Path, job_id: str):
    workspace = _video_evidence_parent(tmp_path, job_id)
    authoring_request = prepare_authoring_job(workspace.root, "draft-video-report").request
    authoring_path = tmp_path / f"{job_id}-authoring-response.json"
    _write_model(authoring_path, _author_response(authoring_request))
    _import_authoring_fixture(workspace.root, authoring_path)
    _block_initial_draft(
        tmp_path,
        workspace,
        job_id=job_id,
        category="content-integrity",
    )
    frozen = _freeze_pre_35_evidence(workspace)
    return workspace, frozen


def test_frozen_pre_35_bundle_validates_and_recovery_reuses_only_legacy_inputs(
    tmp_path: Path,
) -> None:
    parent, (manifest, request, media, transcript, response) = _legacy_blocked_video_parent(
        tmp_path, "legacy-v1-parent"
    )
    assert load_job_yaml(parent.job_yaml).primary_input is None
    assert (
        "atomic_proposition_contract"
        not in json.loads(parent.evidence_request_json.read_text(encoding="utf-8"))[
            "analyzer_contract"
        ]
    )
    assert manifest.evidence_bundle_fingerprint == _pre_35_bundle_fingerprint(
        transcript, media, request, response
    )

    raw_artifact = verify_acquired_bundle(parent, load_job_yaml(parent.job_yaml))
    normalized = load_normalization_result(parent, load_job_yaml(parent.job_yaml), raw_artifact)
    transcription = load_transcription_result(
        parent, load_job_yaml(parent.job_yaml), normalized.media_manifest
    )
    loaded = load_evidence_result(
        parent,
        load_job_yaml(parent.job_yaml),
        normalized.media_manifest,
        transcription.transcript_manifest,
        transcription.segments,
    )
    assert (
        loaded.evidence_manifest.evidence_bundle_fingerprint == manifest.evidence_bundle_fingerprint
    )
    legacy_fixed_fingerprint = _pre_35_fixed_evidence_fingerprint(
        load_job_yaml(parent.job_yaml).job_id,
        request,
        loaded.evidence_manifest,
        loaded.evidence_records,
        loaded.ambiguity_records,
    )
    assert not fixed_evidence_identity_matches(
        job_id=load_job_yaml(parent.job_yaml).job_id,
        evidence_request=request,
        evidence_manifest=loaded.evidence_manifest,
        evidence_records=list(loaded.evidence_records),
        ambiguity_records=list(loaded.ambiguity_records),
        stored_fingerprint=legacy_fixed_fingerprint,
    )
    assert validate_job(parent.root)["state"] == JobState.BLOCKED.value
    assert validate_job(parent.root, deep=True)["state"] == JobState.BLOCKED.value

    request_bytes = parent.evidence_request_json.read_bytes()
    request_payload = json.loads(request_bytes)
    request_payload["transcript_bundle_fingerprint"] = "0" * 64
    request_payload["media_bundle_fingerprint"] = "0" * 64
    parent.evidence_request_json.write_bytes(pretty_json_bytes(request_payload))
    assert validate_job(parent.root)["state"] == JobState.BLOCKED.value
    try:
        validate_job(parent.root, deep=True)
    except VepError as exc:
        assert exc.code == "EVIDENCE_REQUEST_STALE"
    else:
        raise AssertionError("deep validation must reject the frozen request fingerprints")
    parent.evidence_request_json.write_bytes(request_bytes)

    manifest_payload = json.loads(parent.evidence_manifest_json.read_text(encoding="utf-8"))
    manifest_payload["evidence_bundle_fingerprint"] = "0" * 64
    parent.evidence_manifest_json.write_bytes(pretty_json_bytes(manifest_payload))
    if parent.evidence_candidate_manifest_json.exists():
        candidate_payload = json.loads(
            parent.evidence_candidate_manifest_json.read_text(encoding="utf-8")
        )
        candidate_payload["evidence_bundle_fingerprint"] = "0" * 64
        parent.evidence_candidate_manifest_json.write_bytes(pretty_json_bytes(candidate_payload))
    stage_payload = json.loads(parent.evidence_stage_manifest_json.read_text(encoding="utf-8"))
    stage_payload["configuration_sha256"] = "0" * 64
    parent.evidence_stage_manifest_json.write_bytes(pretty_json_bytes(stage_payload))
    assert validate_job(parent.root)["state"] == JobState.BLOCKED.value
    try:
        validate_job(parent.root, deep=True)
    except VepError as exc:
        assert exc.code == "EVIDENCE_MANIFEST_MISMATCH"
    else:
        raise AssertionError("deep validation must reject the frozen semantic fingerprint")

    parent_before = _file_bytes(parent.root)
    recovered = recover_job(
        parent.root,
        "legacy-v1-recovery-01",
        confirm=True,
        basis="restart semantic processing from frozen legacy v1 evidence",
    )
    child = recovered.workspace
    assert validate_job(child.root)["state"] == JobState.TRANSCRIBED.value
    child_job = load_job_yaml(child.job_yaml)
    assert child_job.primary_input is not None
    assert child_job.primary_input.kind is PrimaryInputKind.VIDEO
    assert requires_transcript_refinement(child_job) is True
    assert _file_bytes(parent.root) == parent_before
    assert not child.evidence_manifest_json.exists()
    for directory in ("refinement", "evidence", "authoring", "human-review", "approval", "exports"):
        assert not any(path.is_file() for path in (child.root / directory).rglob("*"))


def _freeze_pre_35_request(path: Path, request_type):
    payload = json.loads(path.read_text(encoding="utf-8"))
    analyzer_contract = payload["fixed_evidence"]["evidence_request"]["analyzer_contract"]
    assert analyzer_contract == {
        "name": "video-evidence-analyzer",
        "version": "1.0",
        "response_schema_version": "1.0",
        "atomic_proposition_contract": None,
    }
    analyzer_contract.pop("atomic_proposition_contract")
    stored_payload = {key: value for key, value in payload.items() if key != "request_fingerprint"}
    stored_fingerprint = fingerprint(stored_payload)
    payload["request_fingerprint"] = stored_fingerprint
    path.write_bytes(pretty_json_bytes(payload))
    return request_type.model_validate(payload), stored_fingerprint, payload


def _request_fingerprint_without_atomic_contract(request) -> str:
    payload = request.model_dump(mode="json", exclude={"request_fingerprint"})
    analyzer_contract = request.fixed_evidence.evidence_request.analyzer_contract.model_dump(
        mode="json", exclude_none=True
    )
    analyzer_contract.pop("atomic_proposition_contract", None)
    payload["fixed_evidence"]["evidence_request"]["analyzer_contract"] = analyzer_contract
    return fingerprint(payload)


def test_frozen_pre_35_authoring_requests_match_only_absent_atomic_contract(
    tmp_path: Path,
) -> None:
    parent, _frozen = _legacy_blocked_video_parent(tmp_path, "legacy-v1-requests")
    request_specs = (
        ("author-v001", AuthoringRequestManifest),
        ("audit-v001", AuditRequestManifest),
        ("revision-v002", RevisionRequestManifest),
    )

    for name, request_type in request_specs:
        request, stored_fingerprint, raw_payload = _freeze_pre_35_request(
            parent.safe_path(f"authoring/requests/{name}.json"),
            request_type,
        )
        assert raw_payload["fixed_evidence"]["evidence_request"]["analyzer_contract"] == {
            "name": "video-evidence-analyzer",
            "version": "1.0",
            "response_schema_version": "1.0",
        }
        assert request.request_fingerprint == stored_fingerprint
        assert (
            request.fixed_evidence.evidence_request.analyzer_contract.atomic_proposition_contract
            is None
        )
        assert _request_fingerprint(request) != stored_fingerprint
        assert not _request_fingerprint_matches(request)
        _validate_request_fingerprint(request)


def test_modern_request_fingerprint_rejects_absent_atomic_contract_alternate(
    tmp_path: Path,
) -> None:
    workspace = _video_evidence_parent(tmp_path, "modern-request")
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    modern_evidence_request = request.fixed_evidence.evidence_request.model_copy(
        update={
            "analyzer_contract": AnalyzerContract(
                atomic_proposition_contract=ATOMIC_PROPOSITION_RULE,
            )
        }
    )
    modern_fixed = request.fixed_evidence.model_copy(
        update={"evidence_request": modern_evidence_request}
    )
    modern = request.model_copy(update={"fixed_evidence": modern_fixed})
    modern_fingerprint = _request_fingerprint(modern)
    modern = modern.model_copy(update={"request_fingerprint": modern_fingerprint})
    legacy_fingerprint = _request_fingerprint_without_atomic_contract(modern)

    assert (
        modern.fixed_evidence.evidence_request.analyzer_contract.atomic_proposition_contract
        is not None
    )
    assert _request_fingerprint_matches(modern)
    assert modern_fingerprint != legacy_fingerprint
    assert not _request_fingerprint_matches(
        modern.model_copy(update={"request_fingerprint": legacy_fingerprint})
    )


def test_none_request_fingerprint_accepts_canonical_and_absent_contract_only(
    tmp_path: Path,
) -> None:
    workspace = _video_evidence_parent(tmp_path, "transitional-request")
    request = prepare_authoring_job(workspace.root, "draft-video-report").request
    assert (
        request.fixed_evidence.evidence_request.analyzer_contract.atomic_proposition_contract
        is None
    )

    canonical = _request_fingerprint(request)
    legacy = _request_fingerprint_without_atomic_contract(request)
    assert canonical != legacy
    assert _request_fingerprint_matches(
        request.model_copy(update={"request_fingerprint": canonical})
    )
    assert not _request_fingerprint_matches(
        request.model_copy(update={"request_fingerprint": legacy})
    )
    assert not _request_fingerprint_matches(
        request.model_copy(update={"request_fingerprint": "0" * 64})
    )
