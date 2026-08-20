from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from video_evidence_pipeline.authoring.pipeline import load_skill_snapshot as authoring_loader
from video_evidence_pipeline.domain.authoring import SkillSnapshot as AuthoringSkillSnapshot
from video_evidence_pipeline.domain.evidence import (
    EvidenceConfidence,
    EvidenceRecord,
    InterpretationLevel,
    ReviewStatus,
    SourceRef,
    SourceRefKind,
)
from video_evidence_pipeline.domain.job import (
    AssignmentSpec,
    JobSpec,
    OutputSpec,
    SourceKind,
    SourceSpec,
    job_fingerprint,
    job_to_yaml,
)
from video_evidence_pipeline.domain.semantic import SkillSnapshot
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import ExitCode, VepError
from video_evidence_pipeline.evidence import pipeline as evidence_pipeline
from video_evidence_pipeline.semantic.skills import (
    load_skill_snapshot,
    same_skill_snapshot_except_lifecycle,
)
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.transcription import refinement as refinement_pipeline

_NOW = "2026-08-20T00:00:00Z"


def _job(*, purpose: str | None = None, audience: str | None = None) -> JobSpec:
    return JobSpec(
        job_id="issue49-semantic-contract",
        created_at=_NOW,
        source=SourceSpec(kind=SourceKind.LOCAL_FILE, locator="source.mp4"),
        assignment=AssignmentSpec(
            task_type="summary",
            language="ja",
            purpose=purpose,
            audience=audience,
        ),
        output=OutputSpec(format="markdown"),
    )


def _draft_skill_repository(tmp_path: Path, *, resources: str = "[]") -> JobWorkspace:
    root = tmp_path / "repository"
    skill_root = root / ".agents" / "skills" / "bounded-skill"
    eval_root = root / ".agents" / "skill-evals"
    skill_root.mkdir(parents=True)
    eval_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: bounded-skill\ndescription: bounded test Skill\n---\n\nProcedure.\n",
        encoding="utf-8",
    )
    (eval_root / "bounded-skill.toml").write_text(
        'schema_version = "1.0"\nskill_id = "bounded-skill"\n', encoding="utf-8"
    )
    (root / ".agents" / "skill-system.toml").write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                "",
                "[[skills]]",
                'id = "bounded-skill"',
                'path = ".agents/skills/bounded-skill/SKILL.md"',
                'lifecycle = "draft"',
                "automatic_trigger = false",
                'side_effect_policy = "instruction-only"',
                'eval_path = ".agents/skill-evals/bounded-skill.toml"',
                'owner = "owner"',
                f"resources = {resources}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return JobWorkspace.create(root, "issue49-skill-loader")


def _promote_fake_skill(workspace: JobWorkspace, *, bundle_fingerprint: str) -> None:
    root = workspace.jobs_root.parent.parent
    registry_path = root / ".agents" / "skill-system.toml"
    registry = registry_path.read_text(encoding="utf-8")
    registry_path.write_text(
        registry.replace('lifecycle = "draft"', 'lifecycle = "active"').replace(
            'eval_path = ".agents/skill-evals/bounded-skill.toml"',
            "\n".join(
                [
                    'eval_path = ".agents/skill-evals/bounded-skill.toml"',
                    'eval_result_path = ".agents/skill-eval-results/bounded-skill.toml"',
                ]
            ),
        ),
        encoding="utf-8",
    )
    skill_path = root / ".agents" / "skills" / "bounded-skill" / "SKILL.md"
    eval_path = root / ".agents" / "skill-evals" / "bounded-skill.toml"
    result_path = root / ".agents" / "skill-eval-results" / "bounded-skill.toml"
    result_path.parent.mkdir()
    canonical_skill = hashlib.sha256(skill_path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    canonical_eval = hashlib.sha256(eval_path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    result_path.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'skill_id = "bounded-skill"',
                'skill_path = ".agents/skills/bounded-skill/SKILL.md"',
                f'skill_content_sha256 = "{canonical_skill}"',
                'eval_definition_path = ".agents/skill-evals/bounded-skill.toml"',
                f'eval_definition_sha256 = "{canonical_eval}"',
                f'resource_bundle_fingerprint = "{bundle_fingerprint}"',
                f'evaluated_commit_sha = "{"a" * 40}"',
                'side_effect_policy = "instruction-only"',
                "promotion_eligible = true",
                "p0_count = 0",
                "p1_count = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_absent_assignment_intent_retains_historical_projection() -> None:
    job = _job()
    legacy_assignment = job.assignment.model_dump(mode="json")
    legacy_assignment.pop("length", None)
    legacy = {
        "schema_version": "1.0",
        "job_id": job.job_id,
        "created_at": job.model_dump(mode="json")["created_at"],
        "source": job.source.model_dump(mode="json"),
        "assignment": legacy_assignment,
        "output": job.output.model_dump(mode="json"),
    }

    assert "purpose" not in legacy_assignment
    assert "audience" not in legacy_assignment
    assert job_fingerprint(job) == fingerprint(legacy)
    assert "purpose:" not in job_to_yaml(job)
    assert "audience:" not in job_to_yaml(job)


def test_assignment_intent_changes_new_job_identity_and_yaml() -> None:
    legacy = _job()
    intended = _job(purpose="重要な実務判断を伝える", audience="研修参加者")

    assert job_fingerprint(intended) != job_fingerprint(legacy)
    yaml = job_to_yaml(intended)
    assert "purpose: 重要な実務判断を伝える" in yaml
    assert "audience: 研修参加者" in yaml


def test_authoring_reexports_the_single_common_skill_snapshot_type() -> None:
    assert AuthoringSkillSnapshot is SkillSnapshot
    assert authoring_loader is load_skill_snapshot


def test_resource_bytes_and_manifest_are_bound_to_snapshot(tmp_path: Path) -> None:
    workspace = _draft_skill_repository(
        tmp_path,
        resources='[{ path = "references/rule.md", type = "reference" }]',
    )
    resource = workspace.jobs_root.parent.parent / ".agents/skills/bounded-skill/references/rule.md"
    resource.parent.mkdir()
    resource.write_text("first rule\n", encoding="utf-8")
    first_bytes = resource.read_bytes()

    first = load_skill_snapshot(workspace, "bounded-skill", allowed=frozenset({"bounded-skill"}))
    resource.write_text("changed rule\n", encoding="utf-8")
    second = load_skill_snapshot(workspace, "bounded-skill", allowed=frozenset({"bounded-skill"}))

    assert first.resources is not None
    assert first.resources[0].sha256 == hashlib.sha256(first_bytes).hexdigest()
    assert first.resource_bundle_fingerprint != second.resource_bundle_fingerprint


def test_active_snapshot_binds_crlf_safe_eval_result_and_evaluated_commit(tmp_path: Path) -> None:
    workspace = _draft_skill_repository(tmp_path)
    empty_bundle = fingerprint([])
    _promote_fake_skill(workspace, bundle_fingerprint=empty_bundle)

    snapshot = load_skill_snapshot(workspace, "bounded-skill", allowed=frozenset({"bounded-skill"}))

    assert snapshot.lifecycle == "active"
    assert snapshot.resources == []
    assert snapshot.resource_bundle_fingerprint == empty_bundle
    assert snapshot.eval_result_sha256 is not None
    assert snapshot.evaluated_commit_sha == "a" * 40
    frozen_draft = snapshot.model_copy(
        update={
            "lifecycle": "draft",
            "eval_result_sha256": None,
            "evaluated_commit_sha": None,
        }
    )
    assert same_skill_snapshot_except_lifecycle(frozen_draft, snapshot)
    changed_result = snapshot.model_copy(update={"eval_result_sha256": "f" * 64})
    assert not same_skill_snapshot_except_lifecycle(snapshot, changed_result)


def test_active_snapshot_rejects_stale_resource_evaluation(tmp_path: Path) -> None:
    workspace = _draft_skill_repository(tmp_path)
    _promote_fake_skill(workspace, bundle_fingerprint="0" * 64)

    with pytest.raises(VepError) as error:
        load_skill_snapshot(workspace, "bounded-skill", allowed=frozenset({"bounded-skill"}))

    assert error.value.code == "SKILL_EVAL_RESULT_INVALID"


@pytest.mark.parametrize(
    "declaration",
    [
        '[{ path = "../escape.md", type = "reference" }]',
        '[{ path = "/absolute.md", type = "reference" }]',
        (
            '[{ path = "references/a.md", type = "reference" }, '
            '{ path = "references/a.md", type = "reference" }]'
        ),
    ],
)
def test_resource_manifest_rejects_escape_absolute_and_duplicate_paths(
    tmp_path: Path, declaration: str
) -> None:
    workspace = _draft_skill_repository(tmp_path, resources=declaration)

    with pytest.raises(VepError) as error:
        load_skill_snapshot(workspace, "bounded-skill", allowed=frozenset({"bounded-skill"}))

    assert error.value.code == "SKILL_REGISTRY_INVALID"


def test_resource_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    workspace = _draft_skill_repository(
        tmp_path,
        resources='[{ path = "references/rule.md", type = "reference" }]',
    )
    skill_root = workspace.jobs_root.parent.parent / ".agents/skills/bounded-skill"
    target = tmp_path / "outside.md"
    target.write_text("outside\n", encoding="utf-8")
    references = skill_root / "references"
    references.mkdir()
    link = references / "rule.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(VepError) as error:
        load_skill_snapshot(workspace, "bounded-skill", allowed=frozenset({"bounded-skill"}))

    assert error.value.code == "SKILL_SNAPSHOT_INVALID"


def test_known_roles_are_allowed_unknown_roles_fail_closed_and_legacy_flags_remain() -> None:
    source = SourceRef(
        kind=SourceRefKind.TRANSCRIPT_SEGMENT,
        ref_id="T000001",
        content_sha256="0" * 64,
    )
    base = {
        "evidence_id": "E000001",
        "observation": "担当者は金曜日までに提出する。",
        "interpretation_level": InterpretationLevel.EXPLICIT_STATEMENT,
        "confidence": EvidenceConfidence.HIGH,
        "source_refs": [source],
        "review_status": ReviewStatus.UNREVIEWED,
        "asserted_as_video_fact": True,
    }

    accepted = EvidenceRecord(**base, flags=["legacy:kept", "role:action-item", "role:deadline"])
    assert accepted.flags == ["legacy:kept", "role:action-item", "role:deadline"]
    with pytest.raises(ValidationError, match="unknown evidence information role"):
        EvidenceRecord(**base, flags=["role:important"])


@pytest.mark.parametrize(
    ("validator", "error_code"),
    [
        (refinement_pipeline.validate_refinement_import_skill, "REFINEMENT_SKILL_REQUIRED"),
        (evidence_pipeline.validate_evidence_import_skill, "EVIDENCE_SKILL_REQUIRED"),
    ],
)
def test_new_semantic_import_rejects_legacy_request_without_skill(
    validator: object, error_code: str, tmp_path: Path
) -> None:
    workspace = _draft_skill_repository(tmp_path)

    with pytest.raises(VepError) as error:
        validator(  # type: ignore[operator]
            workspace,
            SimpleNamespace(skill=None),
            SimpleNamespace(),
            allow_fixture=False,
        )

    assert error.value.code == error_code


@pytest.mark.parametrize(
    ("module", "validator", "allowed"),
    [
        (
            refinement_pipeline,
            refinement_pipeline.validate_refinement_import_skill,
            refinement_pipeline.REFINEMENT_SKILLS,
        ),
        (
            evidence_pipeline,
            evidence_pipeline.validate_evidence_import_skill,
            evidence_pipeline.EVIDENCE_SKILLS,
        ),
    ],
)
@pytest.mark.parametrize("gate_error", ["DRAFT_SKILL_FORBIDDEN", "SKILL_SNAPSHOT_STALE"])
def test_new_semantic_import_propagates_current_skill_gate(
    module: object,
    validator: object,
    allowed: frozenset[str],
    gate_error: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _draft_skill_repository(tmp_path)
    frozen = SimpleNamespace(skill_id=next(iter(allowed)))

    def reject_gate(
        actual_workspace: JobWorkspace,
        actual_skill: object,
        *,
        allowed: frozenset[str],
    ) -> None:
        assert actual_workspace is workspace
        assert actual_skill is frozen
        assert allowed == allowed_set
        exit_code = (
            ExitCode.INTEGRITY
            if gate_error == "SKILL_SNAPSHOT_STALE"
            else ExitCode.AUTHORIZATION_OR_STATE
        )
        raise VepError(gate_error, "synthetic gate failure", exit_code)

    allowed_set = allowed
    monkeypatch.setattr(module, "validate_import_skill", reject_gate)
    with pytest.raises(VepError) as error:
        validator(  # type: ignore[operator]
            workspace,
            SimpleNamespace(skill=frozen),
            SimpleNamespace(),
            allow_fixture=False,
        )

    assert error.value.code == gate_error
