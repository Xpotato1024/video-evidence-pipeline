"""Versioned, provider-neutral lecture authoring and review contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    JsonValue,
    StringConstraints,
    model_serializer,
    model_validator,
)

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.authoring import AgentIdentity, AgentRole, AuditSeverity
from video_evidence_pipeline.domain.clarification import ClarificationRecord
from video_evidence_pipeline.domain.delivery import NonBlankBasis, ReviewerIdentity
from video_evidence_pipeline.domain.evidence import (
    AmbiguityRecord,
    AssignmentRequirement,
    SourceRef,
)
from video_evidence_pipeline.domain.identifiers import JobId, Sha256
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.domain.study_bundle import portable_path

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
ArtifactIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"),
    AfterValidator(portable_path),
]
Version = Annotated[str, StringConstraints(pattern=r"^v(00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=65536)]
Language = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")]
SourceIds = Annotated[list[Identifier], Field(min_length=1, max_length=128)]
OutputMode = Literal["worked_explanation", "submission_answer"]
AuditKind = Literal["content", "visual"]
Topic = Literal[
    "concepts",
    "definitions",
    "derivations",
    "conditions",
    "teacher_emphasis",
    "assignments",
    "next_lesson",
    "worked_examples",
    "visuals",
    "prior_lessons",
]
RENDER_OUTPUTS = {
    "markdown": "lesson.md",
    "mdx": "lesson.mdx",
    "html": "review.html",
    "pdf": "review.pdf",
    "sites": "sites/index.html",
}
REVIEW_ENTRYPOINTS = {
    "content": "content.json",
    "audit_findings": "audit-findings.json",
    "human_review": "human-review-bundle.json",
    "render_receipt": "render-receipt.json",
}
ENTRYPOINTS = {
    "source": RENDER_OUTPUTS["mdx"],
    **{kind: path for kind, path in RENDER_OUTPUTS.items() if kind != "mdx"},
    **REVIEW_ENTRYPOINTS,
}


def review_entrypoints(paths: set[str]) -> dict[str, str]:
    return {
        name: path
        for name, path in ENTRYPOINTS.items()
        if name in REVIEW_ENTRYPOINTS or path in paths
    }


def payload_fingerprint(value: StrictModel, field: str) -> str:
    return fingerprint(value.model_dump(mode="json", exclude={field}))


class SourceLocator(StrictModel):
    ref_id: Identifier | None = None
    page: Annotated[int, Field(ge=1)] | None = None
    start_ms: Annotated[int, Field(ge=0)] | None = None
    end_ms: Annotated[int, Field(gt=0)] | None = None
    region: tuple[float, float, float, float] | None = None
    session_id: Identifier | None = None
    sha256: Sha256 | None = None
    note: Text | None = None

    @model_validator(mode="after")
    def check_interval(self) -> Self:
        if self.end_ms is not None and (self.start_ms is None or self.end_ms <= self.start_ms):
            raise ValueError("invalid source time interval")
        if self.region is not None:
            x, y, width, height = self.region
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x and 0 < height <= 1 - y):
                raise ValueError("source region uses normalized x, y, width, height")
        return self


class LectureSource(StrictModel):
    id: Identifier
    kind: Literal["current_lecture", "previous_lecture", "clarification", "supplement"]
    title: Text
    locator: SourceLocator
    excerpt: Text | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=128)
    evidence_roles: list[Identifier] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def check_origin(self) -> Self:
        match self.kind:
            case "current_lecture":
                if not self.source_refs or not self.excerpt:
                    raise ValueError("current lecture sources require verified evidence")
            case "previous_lecture":
                if not self.locator.session_id or not self.locator.sha256 or not self.excerpt:
                    raise ValueError(
                        "previous lecture sources require session and artifact binding"
                    )
            case "supplement":
                if not self.locator.note or self.source_refs:
                    raise ValueError("supplement must explain its origin separately")
            case "clarification":
                if not self.locator.note or not self.locator.sha256 or not self.excerpt:
                    raise ValueError("clarification requires a bound source and confirmation text")
        if self.kind != "current_lecture" and self.evidence_roles:
            raise ValueError("only current lecture evidence has evidence roles")
        return self


class ProfileRule(StrictModel):
    scope: Literal["content", "format", "calculation", "diagram", "content_audit", "visual_audit"]
    text: Text


class ProfileSkill(StrictModel):
    id: Identifier
    sha256: Sha256


class AuthoringProfile(StrictModel):
    profile_id: Identifier
    version: Identifier
    language: Language
    rules: list[ProfileRule] = Field(default_factory=list, max_length=100)
    skills: list[ProfileSkill] = Field(default_factory=list, max_length=32)


class LectureInput(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    version: Version
    output_mode: OutputMode
    profile: AuthoringProfile
    previous_context: list[LectureSource] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def check_previous_context(self) -> Self:
        if any(source.kind != "previous_lecture" for source in self.previous_context):
            raise ValueError("previous context must be labeled as previous lecture")
        if len({source.id for source in self.previous_context}) != len(self.previous_context):
            raise ValueError("previous context source IDs must be unique")
        return self


class LectureDocument(StrictModel):
    id: Annotated[Identifier, StringConstraints(max_length=96)]
    path: Text
    sha256: Sha256
    title: Text | None = None

    @model_validator(mode="after")
    def check_local_path(self) -> Self:
        path = Path(self.path)
        if not path.is_absolute():
            portable_path(self.path)
        if (
            ".." in path.parts
            or path.anchor.startswith("\\\\")
            or (path.drive and not path.is_absolute())
        ):
            raise ValueError("document paths must be absolute local or portable relative paths")
        return self


class LectureDocumentIndex(StrictModel):
    job_id: JobId
    sources: list[LectureDocument] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def check_ids(self) -> Self:
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("document IDs must be unique")
        return self


class LectureAsset(StrictModel):
    path: str
    sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=12_000_000)]
    source_refs: SourceIds

    @model_validator(mode="after")
    def check_path(self) -> Self:
        portable_path(self.path)
        return self


class LectureGuidance(StrictModel):
    revision: Literal["1"] = "1"
    authoring: Text
    visual_planning: Text
    visual_audit: Text


class LectureRequest(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    job_id: JobId
    guidance: LectureGuidance | None = None
    source_state: Literal[
        "EVIDENCE_READY",
        "DRAFTED",
        "AUDITED",
        "REVISION_REQUIRED",
        "HUMAN_REVIEW_READY",
        "HUMAN_APPROVED",
        "EXPORTED",
        "APPROVED_ARCHIVE",
        "DOCUMENTS_VERIFIED",
    ]
    request_fingerprint: Sha256
    evidence_bundle_fingerprint: Sha256
    profile_snapshot_sha256: Sha256
    version: Version
    output_mode: OutputMode
    profile: AuthoringProfile
    sources: list[LectureSource] = Field(min_length=1, max_length=10000)
    ambiguities: list[AmbiguityRecord] = Field(default_factory=list, max_length=10000)
    clarifications: list[ClarificationRecord] | None = Field(
        default=None, max_length=1000, exclude_if=lambda value: value is None
    )
    requirements: list[AssignmentRequirement] = Field(default_factory=list, max_length=100)
    crop_assets: list[LectureAsset] = Field(default_factory=list, max_length=10000)
    legacy_draft_sha256: Sha256 | None = None
    external_api_allowed: bool

    @model_serializer(mode="wrap")
    def serialize_frozen_request(self, handler):
        fields = handler(self)
        # The original v2 request fingerprint did not include a null guidance field.
        if self.guidance is None:
            fields.pop("guidance", None)
        return fields


class Equation(StrictModel):
    id: ArtifactIdentifier
    tex: Text
    symbol_table: dict[Text, Text] = Field(max_length=128)
    assumptions: list[Text] = Field(max_length=128)
    source_refs: SourceIds


class Figure(StrictModel):
    id: ArtifactIdentifier
    kind: Literal["vega-lite", "mermaid", "geometry", "source_crop"]
    spec: dict[str, JsonValue]
    alt: Text
    caption: Text
    source_refs: SourceIds
    factuality: Literal["source_crop", "derived_diagram", "theoretical", "measured", "illustrative"]

    @model_validator(mode="after")
    def check_factuality(self) -> Self:
        if (self.kind == "source_crop") != (self.factuality == "source_crop"):
            raise ValueError("source crops must identify their factuality")
        if (
            self.kind == "vega-lite"
            and self.factuality == "theoretical"
            and not self.spec.get("calculation")
        ):
            raise ValueError("theoretical charts require their calculation conditions")
        return self


class WorkedStep(StrictModel):
    id: ArtifactIdentifier
    title: Text
    rationale: Text
    symbolic: Text
    substitution: Text | None = None
    intermediate: Text | None = None
    result: Text
    source_refs: SourceIds


class ReferencedBlock(StrictModel):
    id: ArtifactIdentifier
    source_refs: SourceIds


class HeadingBlock(ReferencedBlock):
    type: Literal["heading"]
    text: Text
    level: Literal[2, 3, 4] = 2


class ParagraphBlock(ReferencedBlock):
    type: Literal["paragraph"]
    text: Text


class BackgroundBlock(ReferencedBlock):
    type: Literal["background"]
    title: Text
    paragraphs: list[Text] = Field(min_length=1, max_length=32)


class CalloutBlock(ReferencedBlock):
    type: Literal["callout"]
    title: Text
    text: Text


class TableBlock(ReferencedBlock):
    type: Literal["table"]
    caption: Text
    headers: list[Text] = Field(min_length=1, max_length=24)
    rows: list[list[Text]] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def check_width(self) -> Self:
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("table rows must match the header width")
        return self


class EquationBlock(ReferencedBlock):
    type: Literal["equation"]
    equation_id: ArtifactIdentifier
    numbered: bool = False


class FigureBlock(ReferencedBlock):
    type: Literal["figure"]
    figure_id: ArtifactIdentifier


class BulletListBlock(ReferencedBlock):
    type: Literal["bullets"]
    title: Text
    items: list[Text] = Field(min_length=1, max_length=128)


class ComparisonCase(StrictModel):
    label: Text
    items: list[Text] = Field(min_length=1, max_length=128)


class ComparisonBlock(ReferencedBlock):
    type: Literal["comparison"]
    title: Text
    cases: list[ComparisonCase] = Field(min_length=2, max_length=12)


class OperationStep(ReferencedBlock):
    type: Literal["operation"]
    title: Text
    rationale: Text
    expression: Text | None = None
    numbered: bool = False

    @model_validator(mode="after")
    def check_numbered_expression(self) -> Self:
        if self.numbered and not self.expression:
            raise ValueError("a numbered operation requires an expression")
        return self


class StepsBlock(ReferencedBlock):
    type: Literal["steps"]
    steps: list[WorkedStep | OperationStep] = Field(min_length=1, max_length=128)


class ProcedureStep(OperationStep):
    explanation: Text
    figure_id: ArtifactIdentifier | None = None


class ProcedureBlock(StepsBlock):
    type: Literal["procedure"]
    kind: Literal["calculation", "drawing"]
    steps: list[ProcedureStep] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def check_drawing_stages(self) -> Self:
        if self.kind == "drawing":
            figures = {step.figure_id for step in self.steps}
            if None in figures or len(figures) != len(self.steps):
                raise ValueError("each drawing step requires its own stage figure")
        return self


Block = Annotated[
    HeadingBlock
    | ParagraphBlock
    | BackgroundBlock
    | CalloutBlock
    | TableBlock
    | EquationBlock
    | FigureBlock
    | BulletListBlock
    | ComparisonBlock
    | StepsBlock
    | ProcedureBlock,
    Field(discriminator="type"),
]


class Coverage(StrictModel):
    topic: Topic
    status: Literal["covered", "unresolved", "not_applicable"]
    block_ids: list[Identifier] = Field(max_length=128)
    note: Text | None = None

    @model_validator(mode="after")
    def check_status(self) -> Self:
        if self.status == "covered" and not self.block_ids:
            raise ValueError("covered topic must reference a block")
        if self.status != "covered" and not self.note:
            raise ValueError("a coverage gap requires an explanation")
        return self


class RichContent(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    title: Text
    language: Language
    output_mode: OutputMode
    request_fingerprint: Sha256
    author: AgentIdentity
    sources: list[LectureSource] = Field(min_length=1, max_length=10000)
    blocks: list[Block] = Field(min_length=1, max_length=2048)
    equations: list[Equation] = Field(max_length=512)
    figures: list[Figure] = Field(max_length=55)
    coverage: list[Coverage] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def check_references(self) -> Self:
        if self.author.role != AgentRole.AUTHOR:
            raise ValueError("lecture authoring response requires an author identity")
        steps = [
            step for block in self.blocks if isinstance(block, StepsBlock) for step in block.steps
        ]
        referenced = [*self.blocks, *self.equations, *self.figures, *steps]
        identities = [item.id for item in [*self.sources, *referenced]]
        if len(set(identities)) != len(identities):
            raise ValueError("lecture identifiers must be unique across sources and artifacts")
        sources = {source.id for source in self.sources}
        for item in referenced:
            if not set(item.source_refs) <= sources or len(set(item.source_refs)) != len(
                item.source_refs
            ):
                raise ValueError("all source references must resolve and be unique")
        for cls, id_field, objects in (
            (EquationBlock, "equation_id", self.equations),
            (FigureBlock, "figure_id", self.figures),
        ):
            actual = {
                getattr(block, id_field): block for block in self.blocks if isinstance(block, cls)
            }
            nested = set()
            if cls is FigureBlock:
                figures = {figure.id: figure for figure in self.figures}
                for block in self.blocks:
                    if isinstance(block, ProcedureBlock):
                        for step in block.steps:
                            if step.figure_id is None:
                                continue
                            figure = figures.get(step.figure_id)
                            if figure is None or not set(figure.source_refs) <= set(
                                step.source_refs
                            ):
                                raise ValueError(
                                    "stage figure must resolve and be cited by its step"
                                )
                            nested.add(step.figure_id)
            if set(actual) | nested != {item.id for item in objects}:
                raise ValueError("every equation and figure must resolve to a placed block")
            if any(
                set(actual[item.id].source_refs) != set(item.source_refs)
                for item in objects
                if item.id in actual
            ):
                raise ValueError("equation or figure provenance disagrees with its block")
        if any(
            set(step.source_refs) - set(block.source_refs)
            for block in self.blocks
            if isinstance(block, StepsBlock)
            for step in block.steps
        ):
            raise ValueError("step provenance must belong to its enclosing block")
        block_ids = {block.id for block in self.blocks}
        if len({item.topic for item in self.coverage}) != len(self.coverage) or any(
            not set(item.block_ids) <= block_ids for item in self.coverage
        ):
            raise ValueError("coverage entries must be unique and reference known blocks")
        return self


class LectureAuditFinding(StrictModel):
    severity: AuditSeverity
    target_id: Identifier
    detail: Text
    source_refs: list[Identifier] = Field(default_factory=list, max_length=128)


class LectureAuditResponse(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    kind: AuditKind
    request_fingerprint: Sha256
    agent: AgentIdentity
    checked_blocks: list[Identifier] = Field(default_factory=list, max_length=2048)
    checked_figures: list[Identifier] = Field(default_factory=list, max_length=512)
    checked_equations: list[Identifier] = Field(default_factory=list, max_length=512)
    findings: list[LectureAuditFinding] = Field(max_length=2048)


class InventoryFile(StrictModel):
    path: str
    size_bytes: Annotated[int, Field(ge=1, le=128 * 1024 * 1024)]
    sha256: Sha256

    @model_validator(mode="after")
    def check_path(self) -> Self:
        portable_path(self.path)
        return self


class RichDeliverableManifest(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    job_id: JobId
    version: Version
    state: Literal["HUMAN_REVIEW_READY"] = "HUMAN_REVIEW_READY"
    content_sha256: Sha256
    evidence_bundle_fingerprint: Sha256
    profile_snapshot_sha256: Sha256
    approval_target_sha256: Sha256
    inventory: list[InventoryFile] = Field(min_length=1, max_length=4096)
    entrypoints: dict[str, str]

    @model_validator(mode="after")
    def check_inventory(self) -> Self:
        paths = [item.path for item in self.inventory]
        if paths != sorted(set(paths)) or len(set(str.casefold(path) for path in paths)) != len(
            paths
        ):
            raise ValueError("inventory must be sorted with unique portable paths")
        path_set = set(paths)
        if (
            self.entrypoints != review_entrypoints(path_set)
            or not set(self.entrypoints.values()) <= path_set
            or path_set.isdisjoint(RENDER_OUTPUTS.values())
        ):
            raise ValueError("required review files are missing")
        if "bundle-manifest.json" in paths:
            raise ValueError("a manifest cannot inventory itself")
        if (
            next(item.sha256 for item in self.inventory if item.path == ENTRYPOINTS["content"])
            != self.content_sha256
        ):
            raise ValueError("content SHA-256 does not match the inventory")
        if self.approval_target_sha256 != payload_fingerprint(self, "approval_target_sha256"):
            raise ValueError("review package fingerprint mismatch")
        return self


class LectureApproval(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    state: Literal["HUMAN_APPROVED"] = "HUMAN_APPROVED"
    job_id: JobId
    version: Version
    approval_target_sha256: Sha256
    confirmed_by_user: Literal[True] = True
    reviewer: ReviewerIdentity
    basis: NonBlankBasis
    approved_at: UtcDateTime
    record_sha256: Sha256
