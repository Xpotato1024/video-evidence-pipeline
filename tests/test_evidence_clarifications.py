from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path

from test_lecture_delivery import _fixture_content
from test_transcription_evidence import _response, _write_response
from typer.testing import CliRunner

from video_evidence_pipeline.application import (
    _import_evidence_fixture,
    _import_transcript_refinement_fixture,
    acquire_job,
    authorize_job,
    initialize_job,
    normalize_job,
    prepare_evidence_job,
    prepare_transcript_refinement_job,
    transcribe_job,
)
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.delivery import lecture
from video_evidence_pipeline.domain.evidence import (
    AmbiguityRecord,
    AmbiguityStatus,
    AnalyzerExecutionMode,
    EvidenceConfidence,
    EvidenceRecord,
    InterpretationLevel,
    UncertaintyAssessment,
)
from video_evidence_pipeline.domain.job import (
    PrimaryInputKind,
    SourceKind,
    SupportingInputKind,
    SupportingInputSpec,
)
from video_evidence_pipeline.domain.lecture import RichContent
from video_evidence_pipeline.domain.refinement import (
    RefinementExecutionMode,
    RefinementResponseManifest,
    RefinerIdentity,
)
from video_evidence_pipeline.domain.serialization import pretty_json_bytes


def test_notes_remain_attributed_and_clarifications_freeze_into_new_review(tmp_path: Path):
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("Someone should send the report. Is Friday confirmed?", encoding="utf-8")
    notes = tmp_path / "notes.md"
    notes.write_text("Avery? Friday maybe\n", encoding="utf-8")
    shutil.copytree(Path(__file__).parents[1] / ".agents", tmp_path / ".agents")
    workspace, _ = initialize_job(
        tmp_path,
        job_id="review-notes",
        source_kind=SourceKind.LOCAL_FILE,
        source=str(transcript),
        primary_input=PrimaryInputKind.TRANSCRIPT,
        supporting_inputs=[SupportingInputSpec(kind=SupportingInputKind.NOTES, locator=str(notes))],
        task_type="meeting-minutes",
        language="en",
        output_format="markdown",
    )
    authorize_job(
        workspace.root,
        confirm=True,
        basis="synthetic discussion fixture",
        allow_local_processing=True,
        allow_external_api=False,
        confirmer_identity="local-reviewer",
    )
    acquire_job(workspace.root)
    normalize_job(workspace.root)
    transcribe_job(workspace.root)
    refinement = prepare_transcript_refinement_job(workspace.root).request
    response_file = tmp_path / "response.json"
    response_file.write_bytes(
        pretty_json_bytes(
            RefinementResponseManifest(
                request_fingerprint=refinement.request_fingerprint,
                refiner=RefinerIdentity(
                    provider="fixture",
                    model="fixture",
                    executor="pytest",
                    version="1",
                    execution_mode=RefinementExecutionMode.TEST_FIXTURE,
                    external_api_used=False,
                ),
                created_at=refinement.created_at + timedelta(seconds=1),
            )
        )
    )
    _import_transcript_refinement_fixture(workspace.root, response_file)
    request = prepare_evidence_job(workspace.root).request
    assert request.analyzer_contract.uncertainty_policy is not None
    note_ref = request.approved_artifact_refs[0]
    input_note = workspace.safe_path(request.approved_artifacts[0].relative_storage_path)
    assert input_note.read_text(encoding="utf-8") == "Avery? Friday maybe\n"
    ambiguity = AmbiguityRecord(
        ambiguity_id="A000001",
        description="The note suggests Avery, but mentions both details tentatively.",
        source_refs=[note_ref, request.transcript_refs[0]],
        assessment=UncertaintyAssessment(
            kind="particular_event",
            impact="material",
            question="Who agreed to send the report, and when?",
        ),
    )

    runner = CliRunner()

    def invoke(*args: str, code: str | None = None) -> dict:
        result = runner.invoke(app, [*args, "--json"])
        assert result.exit_code == 0 or code is not None, result.output
        value = json.loads(result.stdout)
        if code is not None:
            assert result.exit_code != 0 and value["error"]["code"] == code, value
        return value

    def publish_evidence(
        current: AmbiguityRecord,
        *,
        evidence: list[EvidenceRecord] | None = None,
    ) -> None:
        _write_response(
            response_file,
            _response(request, ambiguities=[current], evidence=evidence),
        )

    asserted = EvidenceRecord(
        evidence_id="E000001",
        observation="Avery will send the report Friday",
        interpretation_level=InterpretationLevel.EXTERNAL_FACT,
        confidence=EvidenceConfidence.HIGH,
        source_refs=[note_ref, request.transcript_refs[0]],
    )
    publish_evidence(ambiguity, evidence=[asserted])
    invoke(
        "evidence",
        "import",
        str(workspace.root),
        "--response",
        str(response_file),
        code="NOTES_ONLY_EXTERNAL_FACT",
    )

    publish_evidence(
        ambiguity.model_copy(
            update={
                "status": AmbiguityStatus.RESOLVED,
                "resolution": "Avery will send it Friday",
            }
        )
    )
    invoke(
        "evidence",
        "import",
        str(workspace.root),
        "--response",
        str(response_file),
        code="AMBIGUITY_CLARIFICATION_REQUIRED",
    )

    note_attribution = asserted.model_copy(
        update={
            "evidence_id": "E000002",
            "observation": "The supplied note mentions Avery and Friday as possibilities.",
            "interpretation_level": InterpretationLevel.REASONABLE_INFERENCE,
            "confidence": EvidenceConfidence.LOW,
            "source_refs": [note_ref],
        }
    )
    valid_response = _response(
        request,
        execution_mode=AnalyzerExecutionMode.TEST_FIXTURE,
        ambiguities=[ambiguity],
    )
    _write_response(
        response_file,
        valid_response.model_copy(
            update={
                "evidence_records": [*valid_response.evidence_records, note_attribution],
            }
        ),
    )
    _import_evidence_fixture(workspace.root, response_file)
    original = workspace.evidence_manifest_json.read_bytes()
    first_listing = invoke("evidence", "uncertainties", str(workspace.root))
    assert first_listing["items"][0]["effective_status"] == "unresolved"
    assert first_listing["items"][0]["assessment"]["impact"] == "material"

    clarification = {
        "evidence_bundle_fingerprint": first_listing["evidence_bundle_fingerprint"],
        "ambiguity_id": ambiguity.ambiguity_id,
        "decision": "supplemented",
        "rationale": "This workflow example is useful context.",
        "resolution": "The usual reporting window ends Friday.",
    }

    def clarify(*, code: str | None = None, confirm: bool = True) -> dict:
        response_file.write_bytes(pretty_json_bytes(clarification))
        args = [
            "evidence",
            "clarify",
            str(workspace.root),
            "--response",
            str(response_file),
            "--operator",
            "local-reviewer",
        ]
        return invoke(*args, *(["--confirm"] if confirm else []), code=code)

    assert clarify(confirm=False, code="CLARIFICATION_CONFIRM_REQUIRED")
    assert clarify()["reused"] is False
    assert (
        invoke("evidence", "uncertainties", str(workspace.root))["items"][0]["effective_status"]
        == "unresolved"
    )

    clarification.update(
        decision="resolved",
        rationale="The rough note alone is tentative.",
        resolution="Avery sends the report Friday.",
        source_refs=[note_ref.model_dump(mode="json")],
    )
    assert clarify(code="CLARIFICATION_CONFIRMATION_REQUIRED")
    clarification.update(
        rationale="Asked the person responsible for finalizing the task.",
        confirmation={"provided_by": "Meeting organizer", "statement": "Avery agreed to Friday."},
    )
    first_resolution = clarify()
    assert clarify()["reused"] is True
    assert (
        invoke("evidence", "uncertainties", str(workspace.root))["items"][0]["active_resolution"]
        == first_resolution["fingerprint"]
    )
    assert workspace.evidence_manifest_json.read_bytes() == original

    options = tmp_path / "lecture-options.json"
    options.write_bytes(
        pretty_json_bytes(
            {
                "version": "v001",
                "output_mode": "worked_explanation",
                "profile": {"profile_id": "fixture", "version": "1", "language": "en"},
            }
        )
    )
    prepared = tmp_path / "prepared"
    args = [
        "lecture",
        "prepare",
        str(workspace.root),
        "--request",
        str(options),
        "--destination",
        str(prepared),
        "--with-clarifications",
    ]
    assert not invoke(*args)["reused"]
    request_path = prepared / "request.json"
    original_request = request_path.read_bytes()
    frozen = json.loads(original_request)
    assert frozen["ambiguities"][0]["status"] == "unresolved"
    assert [
        source["excerpt"] for source in frozen["sources"] if source["kind"] == "clarification"
    ] == [
        "Avery sends the report Friday.",
    ]
    clarification["resolution"] = "Blake sends the report Thursday."
    clarification["confirmation"]["statement"] = "Correction: Blake agreed to Thursday."
    assert clarify(code="CLARIFICATION_RESOLUTION_CONFLICT")
    clarification["supersedes"] = first_resolution["fingerprint"]
    assert clarify()["reused"] is False
    assert invoke(*args)["reused"] and request_path.read_bytes() == original_request

    newer = tmp_path / "newer"
    args[args.index(str(prepared))] = str(newer)
    revised_options = json.loads(options.read_bytes())
    revised_options["version"] = "v002"
    options.write_bytes(pretty_json_bytes(revised_options))
    assert not invoke(*args)["reused"]
    revised = json.loads((newer / "request.json").read_bytes())
    assert [
        source["excerpt"] for source in revised["sources"] if source["kind"] == "clarification"
    ] == [
        "Blake sends the report Thursday.",
    ]
    typed_request = lecture.validate_lecture_request(newer / "request.json")
    typed_content = RichContent.model_validate(_fixture_content(typed_request))
    audit = lecture._audit_request(typed_request, typed_content, [{"path": "lesson.md"}], "content")
    assert audit["clarifications"][-1]["clarification"]["confirmation"]["statement"] == (
        "Correction: Blake agreed to Thursday."
    )
