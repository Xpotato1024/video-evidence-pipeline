"""Common provenance contracts for repository-owned semantic Skills."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel
from video_evidence_pipeline.domain.identifiers import Sha256

NonBlankOwner = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r".*\S.*"),
]


class SkillResourceSnapshot(StrictModel):
    """One explicitly managed file whose bytes affect semantic execution."""

    relative_path: str = Field(min_length=1, max_length=512)
    sha256: Sha256
    type: Literal["reference", "script", "asset"]

    @field_validator("relative_path")
    @classmethod
    def path_is_canonical(cls, value: str) -> str:
        if "\\" in value or value.startswith("/") or value.endswith("/"):
            raise ValueError("Skill resource path must be a canonical relative POSIX path")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Skill resource path must not escape its Skill root")
        return value

    @model_validator(mode="after")
    def type_matches_directory(self) -> Self:
        expected = {
            "reference": "references",
            "script": "scripts",
            "asset": "assets",
        }[self.type]
        if self.relative_path.split("/", 1)[0] != expected:
            raise ValueError("Skill resource type must match its bundled-resource directory")
        return self


class SkillSnapshot(StrictModel):
    """Frozen semantic contract shared by refinement, evidence, and authoring."""

    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    relative_path: str = Field(pattern=r"^\.agents/skills/[a-z0-9-]+/SKILL\.md$")
    content_sha256: Sha256
    eval_sha256: Sha256
    lifecycle: Literal["draft", "active"]
    automatic_trigger: Literal[False] = False
    side_effect_policy: Literal["instruction-only"] = "instruction-only"
    explicit_selection: Literal[True] = True
    owner: NonBlankOwner
    resources: list[SkillResourceSnapshot] | None = Field(
        default=None,
        max_length=128,
        exclude_if=lambda value: value is None,
    )
    resource_bundle_fingerprint: Sha256 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eval_result_sha256: Sha256 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    evaluated_commit_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def resource_and_promotion_bindings_are_complete(self) -> Self:
        resource_values = (self.resources, self.resource_bundle_fingerprint)
        if (resource_values[0] is None) != (resource_values[1] is None):
            raise ValueError(
                "Skill resource manifest and bundle fingerprint must be recorded together"
            )
        if self.resources is not None:
            paths = [item.relative_path for item in self.resources]
            if paths != sorted(set(paths)):
                raise ValueError("Skill resources must be sorted and unique")
        promotion_values = (self.eval_result_sha256, self.evaluated_commit_sha)
        if (promotion_values[0] is None) != (promotion_values[1] is None):
            raise ValueError(
                "Skill evaluation result and evaluated commit must be recorded together"
            )
        if (
            self.lifecycle == "active"
            and self.resources is not None
            and promotion_values[0] is None
        ):
            raise ValueError("new active Skill snapshots require promotion evidence")
        return self
