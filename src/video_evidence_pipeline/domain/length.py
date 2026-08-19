"""Canonical deterministic character-length contracts and validation."""

from __future__ import annotations

from math import ceil
from typing import Literal, Self

from pydantic import Field, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.identifiers import JobId, Sha256


class CharacterLengthConstraint(StrictModel):
    """Raw assignment values; ``None`` means the author did not specify the bound."""

    min_chars: int | None = Field(default=None, ge=0, le=4_000_000)
    max_chars: int | None = Field(default=None, ge=1, le=4_000_000)
    maximum_adjustments: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if (
            self.min_chars is not None
            and self.max_chars is not None
            and self.min_chars > self.max_chars
        ):
            raise ValueError("minimum character count must not exceed maximum")
        return self


class EffectiveCharacterLengthConstraint(StrictModel):
    """Resolved range used by every deterministic length gate."""

    min_chars: int | None = Field(default=None, ge=0, le=4_000_000)
    max_chars: int | None = Field(default=None, ge=1, le=4_000_000)
    minimum_source: Literal["unspecified", "explicit", "default_80_percent"]
    maximum_adjustments: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if (
            self.min_chars is not None
            and self.max_chars is not None
            and self.min_chars > self.max_chars
        ):
            raise ValueError("effective minimum character count exceeds maximum")
        if self.minimum_source == "unspecified" and self.min_chars is not None:
            raise ValueError("unspecified minimum cannot have a value")
        if self.minimum_source != "unspecified" and self.min_chars is None:
            raise ValueError("resolved minimum source requires a value")
        return self


class CharacterLengthCheck(StrictModel):
    """Machine-produced result suitable for manifests and external requests."""

    actual_chars: int = Field(ge=0, le=4_000_000)
    effective: EffectiveCharacterLengthConstraint
    passed: bool
    below_minimum_by: int = Field(default=0, ge=0, le=4_000_000)
    over_maximum_by: int = Field(default=0, ge=0, le=4_000_000)
    counting_rule: Literal["strip-surrounding-whitespace-then-python-code-points-v1"] = (
        "strip-surrounding-whitespace-then-python-code-points-v1"
    )

    @model_validator(mode="after")
    def result_is_consistent(self) -> Self:
        below = max(0, (self.effective.min_chars or 0) - self.actual_chars)
        over = max(0, self.actual_chars - (self.effective.max_chars or self.actual_chars))
        if self.below_minimum_by != below or self.over_maximum_by != over:
            raise ValueError("character length deltas are inconsistent")
        if self.passed != (below == 0 and over == 0):
            raise ValueError("character length result is inconsistent")
        return self


class CharacterLengthAdjustmentAttempt(StrictModel):
    """One deterministic failed gate; the LLM receives facts, never a counting task."""

    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    target_draft_version: str = Field(pattern=r"^v[0-9]{3}$")
    attempt_number: int = Field(ge=1, le=10)
    response_fingerprint: Sha256
    check: CharacterLengthCheck
    instruction: Literal[
        "Revise only to fit the supplied character range; do not estimate or count characters."
    ] = "Revise only to fit the supplied character range; do not estimate or count characters."
    created_at: UtcDateTime

    @model_validator(mode="after")
    def failed_check_is_required(self) -> Self:
        if self.check.passed:
            raise ValueError("length adjustment attempt requires a failed check")
        if self.attempt_number > self.check.effective.maximum_adjustments:
            raise ValueError("length adjustment attempt exceeds its configured limit")
        return self


def count_characters(text: str) -> int:
    """Count Python Unicode code points after removing surrounding whitespace.

    Internal newlines and spaces, punctuation, ASCII, and Markdown syntax all count.
    No normalization, grapheme folding, or markup removal is performed.
    """

    return len(text.strip())


def resolve_length_constraint(
    raw: CharacterLengthConstraint | None,
) -> EffectiveCharacterLengthConstraint | None:
    if raw is None or (raw.min_chars is None and raw.max_chars is None):
        return None
    if raw.min_chars is not None:
        minimum = raw.min_chars
        source = "explicit"
    elif raw.max_chars is not None:
        minimum = ceil(raw.max_chars * 4 / 5)
        source = "default_80_percent"
    else:  # pragma: no cover - kept explicit for type narrowing
        minimum = None
        source = "unspecified"
    return EffectiveCharacterLengthConstraint(
        min_chars=minimum,
        max_chars=raw.max_chars,
        minimum_source=source,
        maximum_adjustments=raw.maximum_adjustments,
    )


def validate_character_range(
    text: str,
    constraint: EffectiveCharacterLengthConstraint | None,
) -> CharacterLengthCheck | None:
    if constraint is None:
        return None
    actual = count_characters(text)
    below = max(0, (constraint.min_chars or 0) - actual)
    over = max(0, actual - (constraint.max_chars or actual))
    return CharacterLengthCheck(
        actual_chars=actual,
        effective=constraint,
        passed=below == 0 and over == 0,
        below_minimum_by=below,
        over_maximum_by=over,
    )
