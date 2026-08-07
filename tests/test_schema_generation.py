from pathlib import Path

import pytest

from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.schemas.generation import check_schemas, export_schemas


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
