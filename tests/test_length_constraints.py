from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_evidence_pipeline.authoring.pipeline import _enforce_length_gate
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    SourceKind,
    SourceSpec,
)
from video_evidence_pipeline.domain.length import (
    CharacterLengthConstraint,
    count_characters,
    resolve_length_constraint,
    validate_character_range,
)
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.storage.layout import JobWorkspace


@pytest.mark.parametrize(
    ("maximum", "expected"),
    [(300, 240), (301, 241), (100, 80)],
)
def test_maximum_only_resolves_eighty_percent_minimum(maximum: int, expected: int) -> None:
    effective = resolve_length_constraint(CharacterLengthConstraint(max_chars=maximum))
    assert effective is not None
    assert effective.min_chars == expected
    assert effective.max_chars == maximum
    assert effective.minimum_source == "default_80_percent"


def test_explicit_and_open_length_bounds_are_preserved() -> None:
    explicit = resolve_length_constraint(CharacterLengthConstraint(min_chars=7, max_chars=20))
    minimum_only = resolve_length_constraint(CharacterLengthConstraint(min_chars=7))
    assert explicit is not None and explicit.min_chars == 7 and explicit.max_chars == 20
    assert explicit.minimum_source == "explicit"
    assert minimum_only is not None and minimum_only.min_chars == 7
    assert minimum_only.max_chars is None
    assert resolve_length_constraint(None) is None
    assert resolve_length_constraint(CharacterLengthConstraint()) is None


def test_invalid_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        CharacterLengthConstraint(min_chars=11, max_chars=10)


def test_character_count_contract_covers_japanese_whitespace_and_markdown() -> None:
    assert count_characters("  日本語。\n A **B**  ") == len("日本語。\n A **B**")
    effective = resolve_length_constraint(CharacterLengthConstraint(min_chars=5, max_chars=6))
    assert effective is not None
    assert validate_character_range("日本語です", effective).passed is True  # type: ignore[union-attr]
    below = validate_character_range("日本語で", effective)
    over = validate_character_range("日本語ですよね", effective)
    assert below is not None and below.below_minimum_by == 1
    assert over is not None and over.over_maximum_by == 1


def _job(maximum_adjustments: int = 2) -> JobSpec:
    return JobSpec(
        job_id="length-test",
        created_at=datetime.now(UTC),
        source=SourceSpec(kind=SourceKind.LOCAL_FILE, locator="source.mp4"),
        assignment=AssignmentSpec(
            task_type="report",
            language="ja",
            length=CharacterLengthConstraint(
                min_chars=5,
                max_chars=6,
                maximum_adjustments=maximum_adjustments,
            ),
        ),
        output=OutputSpec(format="markdown"),
    )


def _length_gate(workspace: JobWorkspace, job: JobSpec, **kwargs):
    return _enforce_length_gate(
        workspace,
        job,
        response_created_at=job.created_at,
        request_fingerprint="a" * 64,
        **kwargs,
    )


def test_length_adjustment_history_is_bounded_and_does_not_mutate_semantic_state(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(tmp_path, "length-test")
    job = _job()
    with pytest.raises(VepError) as first:
        _length_gate(
            workspace,
            job,
            version="v001",
            draft_markdown="短い",
            response_fingerprint="1" * 64,
            response_payload=b'{"candidate":1}',
        )
    assert first.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    with pytest.raises(VepError) as duplicate:
        _length_gate(
            workspace,
            job,
            version="v001",
            draft_markdown="短い",
            response_fingerprint="1" * 64,
            response_payload=b'{"candidate":1}',
        )
    assert "adjustment: 1/2" in duplicate.value.safe_message
    with pytest.raises(VepError) as second:
        _length_gate(
            workspace,
            job,
            version="v001",
            draft_markdown="長すぎる本文です",
            response_fingerprint="2" * 64,
            response_payload=b'{"candidate":2}',
        )
    assert second.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    latest_request = workspace.safe_path("authoring/length/v001-request.json").read_bytes()
    with pytest.raises(VepError) as non_tail_duplicate:
        _length_gate(
            workspace,
            job,
            version="v001",
            draft_markdown="短い",
            response_fingerprint="1" * 64,
            response_payload=b'{"candidate":1}',
        )
    assert non_tail_duplicate.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    assert "adjustment: 2/2" in non_tail_duplicate.value.safe_message
    assert workspace.safe_path("authoring/length/v001-request.json").read_bytes() == latest_request
    assert (
        len(workspace.safe_path("authoring/length/v001-attempts.jsonl").read_bytes().splitlines())
        == 2
    )
    check, adjustment_count = _length_gate(
        workspace,
        job,
        version="v001",
        draft_markdown="日本語です",
        response_fingerprint="3" * 64,
        response_payload=b'{"candidate":3}',
    )
    assert check is not None and check.passed
    assert adjustment_count == 2
    assert not workspace.transitions_jsonl.exists()


@pytest.mark.parametrize("maximum_adjustments", [1, 2, 3])
def test_last_allowed_successful_correction_is_accepted(
    tmp_path: Path, maximum_adjustments: int
) -> None:
    workspace = JobWorkspace.create(tmp_path, "length-test")
    job = _job(maximum_adjustments)
    for index in range(maximum_adjustments):
        with pytest.raises(VepError) as required:
            _length_gate(
                workspace,
                job,
                version="v001",
                draft_markdown=f"長すぎる候補です{index}",
                response_fingerprint=f"{index + 1:064x}",
                response_payload=f'{{"candidate":{index}}}'.encode(),
            )
        assert required.value.code == "LENGTH_ADJUSTMENT_REQUIRED"
    check, adjustment_count = _length_gate(
        workspace,
        job,
        version="v001",
        draft_markdown="日本語です",
        response_fingerprint="e" * 64,
        response_payload=b'{"candidate":"accepted"}',
    )
    assert check is not None and check.passed
    assert adjustment_count == maximum_adjustments


@pytest.mark.parametrize("maximum_adjustments", [1, 2, 3])
def test_last_allowed_failed_correction_exhausts(tmp_path: Path, maximum_adjustments: int) -> None:
    workspace = JobWorkspace.create(tmp_path, "length-test")
    job = _job(maximum_adjustments)
    for index in range(maximum_adjustments + 1):
        expected = (
            "LENGTH_ADJUSTMENT_EXHAUSTED"
            if index == maximum_adjustments
            else "LENGTH_ADJUSTMENT_REQUIRED"
        )
        with pytest.raises(VepError) as failure:
            _length_gate(
                workspace,
                job,
                version="v001",
                draft_markdown=f"長すぎる候補です{index}",
                response_fingerprint=f"{index + 1:064x}",
                response_payload=f'{{"candidate":{index}}}'.encode(),
            )
        assert failure.value.code == expected
    with pytest.raises(VepError) as too_late:
        _length_gate(
            workspace,
            job,
            version="v001",
            draft_markdown="日本語です",
            response_fingerprint="f" * 64,
            response_payload=b'{"candidate":"too-late"}',
        )
    assert too_late.value.code == "LENGTH_ADJUSTMENT_EXHAUSTED"
