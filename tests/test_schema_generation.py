import json
from pathlib import Path

import pytest

from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.schemas.generation import (
    SCHEMA_MODELS,
    check_schemas,
    export_schemas,
    schema_bytes,
)
from video_evidence_pipeline.schemas.structured_output import structured_output_schema_bytes


def test_schema_export_and_check_are_deterministic(tmp_path: Path) -> None:
    first = export_schemas(tmp_path)
    initial = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    second = export_schemas(tmp_path)
    assert first == second
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == initial
    assert check_schemas(tmp_path) == sorted(initial)


def test_schema_check_detects_drift(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    (tmp_path / "job-spec.schema.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(VepError) as captured:
        check_schemas(tmp_path)
    assert captured.value.code == "SCHEMA_DRIFT"


def test_schema_check_detects_unexpected_generated_schema(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    (tmp_path / "obsolete.schema.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(VepError) as captured:
        check_schemas(tmp_path)
    assert captured.value.code == "SCHEMA_DRIFT"


def test_structured_output_schema_is_strict_without_mutating_canonical_schema() -> None:
    filename = "audit-response-manifest.schema.json"
    canonical_before = schema_bytes(filename, SCHEMA_MODELS[filename])
    structured_first = structured_output_schema_bytes(filename)
    structured_second = structured_output_schema_bytes(filename)

    assert structured_first == structured_second
    assert schema_bytes(filename, SCHEMA_MODELS[filename]) == canonical_before
    assert structured_first != canonical_before

    structured = json.loads(structured_first)
    assert structured["additionalProperties"] is False
    assert structured["required"] == sorted(structured["properties"])

    audit_finding = structured["$defs"]["AuditFinding"]
    assert audit_finding["additionalProperties"] is False
    assert audit_finding["required"] == sorted(audit_finding["properties"])
    assert "evidence_ids" in audit_finding["required"]

    draft_span = structured["$defs"]["DraftSpan"]
    assert draft_span["additionalProperties"] is False
    assert draft_span["required"] == sorted(draft_span["properties"])
