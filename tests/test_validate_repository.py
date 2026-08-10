from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "validate_repository.py"
SPEC = importlib.util.spec_from_file_location("validate_repository", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
RepositoryValidator = MODULE.RepositoryValidator


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def front_matter_document(doc_id: str, canonical_for: str) -> str:
    return f"""---
schema_version: "1.1"
document_id: {doc_id}
title: Test
document_type: governance
status: active
canonical: true
canonical_for: {canonical_for}
owners: [Test]
supersedes: []
superseded_by: null
related_documents: []
---
# Test
"""


def registry_only_document() -> str:
    return """# Adapter

CANONICAL_AGENT_POLICY: `.agents/policy.md`
SKILL_REGISTRY: `.agents/skill-system.toml`
"""


def registry_entry(
    doc_id: str,
    path: str,
    canonical_for: str,
    metadata_mode: str,
) -> dict[str, object]:
    return {
        "document_id": doc_id,
        "path": path,
        "document_type": "governance",
        "status": "active",
        "canonical": True,
        "canonical_for": canonical_for,
        "owners": ["Test"],
        "metadata_mode": metadata_mode,
    }


def make_valid_repo(root: Path) -> set[str]:
    write(root, ".gitattributes", "* text eol=lf\n")
    adapters = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md"]
    entries: list[dict[str, object]] = []
    for adapter in adapters:
        write(root, adapter, registry_only_document())
        did = "DOC-" + adapter.replace("/", "-").replace(".", "-").upper()
        entries.append(registry_entry(did, adapter, adapter, "registry-only"))

    write(root, "docs/test.md", front_matter_document("DOC-TEST", "test"))
    entries.append(registry_entry("DOC-TEST", "docs/test.md", "test", "front-matter"))

    write(
        root,
        "docs/registry/documents.yaml",
        yaml.safe_dump({"documents": entries}, sort_keys=False),
    )
    write(
        root,
        ".agents/skill-system.toml",
        """schema_version = "1.0"
[policy]
activation_mode = "explicit-only"
promotion_threshold = 9
required_eval_cases = 2
[[skills]]
id = "audit"
path = ".agents/skills/audit/SKILL.md"
lifecycle = "active"
automatic_trigger = false
side_effect_policy = "read-only"
eval_path = ".agents/skill-evals/audit.toml"
eval_result_path = ".agents/skill-eval-results/audit.toml"
owner = "Test"
""",
    )
    write(
        root,
        ".agents/skills/audit/SKILL.md",
        "---\nname: audit\ndescription: test\n---\n# audit\n",
    )
    write(
        root,
        ".agents/skill-evals/audit.toml",
        """schema_version = "1.0"
skill_id = "audit"
minimum_score = 9
[[cases]]
id = "a"
prompt = "a"
expected = ["a"]
forbidden = []
criteria = [
  { id = "a1", description = "a1" },
  { id = "a2", description = "a2" },
  { id = "a3", description = "a3" },
  { id = "a4", description = "a4" },
  { id = "a5", description = "a5" },
]
[[cases]]
id = "b"
prompt = "b"
expected = ["b"]
forbidden = []
criteria = [
  { id = "b1", description = "b1" },
  { id = "b2", description = "b2" },
  { id = "b3", description = "b3" },
  { id = "b4", description = "b4" },
  { id = "b5", description = "b5" },
]
""",
    )
    skill_sha = RepositoryValidator._canonical_text_sha256(root / ".agents/skills/audit/SKILL.md")
    eval_sha = RepositoryValidator._canonical_text_sha256(root / ".agents/skill-evals/audit.toml")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Validator Fixture"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "validator@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    evaluated_commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    write(
        root,
        ".agents/skill-eval-results/audit.toml",
        f"""schema_version = "1.0"
skill_id = "audit"
skill_path = ".agents/skills/audit/SKILL.md"
skill_content_sha256 = "{skill_sha}"
eval_definition_path = ".agents/skill-evals/audit.toml"
eval_definition_sha256 = "{eval_sha}"
evaluated_commit_sha = "{evaluated_commit_sha}"
evaluated_at = "2026-08-10T00:00:00Z"
evaluator_identity = "Synthetic evaluator"
evaluator_method = "binary criteria"
finding_summary = "P0=0; P1=0"
side_effect_policy = "read-only"
total_score = 10
maximum_score = 10
threshold = 9
p0_count = 0
p1_count = 0
p2_count = 0
promotion_eligible = true
required_artifacts = ["docs/test.md"]

[owner_review]
owner = "Test"
approved = true
method = "fixture owner review"
reviewed_at = "2026-08-10T00:00:00Z"

[[case_results]]
case_id = "a"
result = "pass"
score = 5
maximum_score = 5
forbidden_observed = false
evidence = ["a evidence"]
criteria = [
  {{ id = "a1", passed = true, evidence = "a1" }},
  {{ id = "a2", passed = true, evidence = "a2" }},
  {{ id = "a3", passed = true, evidence = "a3" }},
  {{ id = "a4", passed = true, evidence = "a4" }},
  {{ id = "a5", passed = true, evidence = "a5" }},
]

[[case_results]]
case_id = "b"
result = "pass"
score = 5
maximum_score = 5
forbidden_observed = false
evidence = ["b evidence"]
criteria = [
  {{ id = "b1", passed = true, evidence = "b1" }},
  {{ id = "b2", passed = true, evidence = "b2" }},
  {{ id = "b3", passed = true, evidence = "b3" }},
  {{ id = "b4", passed = true, evidence = "b4" }},
  {{ id = "b5", passed = true, evidence = "b5" }},
]
""",
    )
    subprocess.run(
        ["git", "add", ".agents/skill-eval-results/audit.toml"],
        cwd=root,
        check=True,
    )
    return set(
        subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )


def finding_codes(root: Path, tracked: set[str]) -> set[str]:
    return {finding.code for finding in RepositoryValidator(root, tracked).validate()}


def test_valid_fixture_passes(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    assert RepositoryValidator(tmp_path, tracked).validate() == []


def test_registry_only_front_matter_fails(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    write(
        tmp_path,
        "AGENTS.md",
        front_matter_document("DOC-AGENTS-MD", "AGENTS.md")
        + "\nCANONICAL_AGENT_POLICY: `.agents/policy.md`\n"
        + "SKILL_REGISTRY: `.agents/skill-system.toml`\n",
    )
    assert "REGISTRY_ONLY_FRONT_MATTER" in finding_codes(tmp_path, tracked)


def test_front_matter_mode_without_front_matter_fails(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    write(tmp_path, "docs/test.md", "# Test\n")
    assert "FRONT_MATTER_MISSING" in finding_codes(tmp_path, tracked)


def test_unknown_metadata_mode_fails(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    registry_path = tmp_path / "docs/registry/documents.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    registry["documents"][-1]["metadata_mode"] = "unknown"
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert "METADATA_MODE_INVALID" in finding_codes(tmp_path, tracked)


def test_missing_metadata_mode_fails(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    registry_path = tmp_path / "docs/registry/documents.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    del registry["documents"][-1]["metadata_mode"]
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert "REGISTRY_FIELD_MISSING" in finding_codes(tmp_path, tracked)


def test_duplicate_canonical_and_secret_fail(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    registry_path = tmp_path / "docs/registry/documents.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    duplicate = dict(registry["documents"][-1])
    duplicate["document_id"] = "DOC-OTHER"
    duplicate["path"] = "docs/other.md"
    registry["documents"].append(duplicate)
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    secret = "sk-" + "a" * 30
    write(
        tmp_path,
        "docs/other.md",
        front_matter_document("DOC-OTHER", "test") + f"\n{secret}\n",
    )
    tracked.add("docs/other.md")
    codes = finding_codes(tmp_path, tracked)
    assert "CANONICAL_FOR_DUPLICATE" in codes
    assert "SECRET_PATTERN" in codes


def test_draft_skill_side_effect_fails(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    skill_system = tmp_path / ".agents/skill-system.toml"
    text = skill_system.read_text(encoding="utf-8").replace(
        'lifecycle = "active"\nautomatic_trigger = false\nside_effect_policy = "read-only"',
        'lifecycle = "draft"\nautomatic_trigger = true\nside_effect_policy = "workspace-write"',
    )
    skill_system.write_text(text, encoding="utf-8")
    codes = finding_codes(tmp_path, tracked)
    assert "DRAFT_SKILL_TRIGGER" in codes
    assert "DRAFT_SKILL_SIDE_EFFECT" in codes


def test_active_skill_requires_evaluation_result(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    result_path.unlink()
    assert "ACTIVE_SKILL_RESULT_MISSING" in finding_codes(tmp_path, tracked)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('activation_mode = "explicit-only"', 'activation_mode = "implicit"'),
        ("promotion_threshold = 9", "promotion_threshold = 8"),
        ("required_eval_cases = 2", "required_eval_cases = 1"),
    ],
)
def test_skill_policy_cannot_weaken_promotion_gate(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    tracked = make_valid_repo(tmp_path)
    registry_path = tmp_path / ".agents/skill-system.toml"
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )
    assert "SKILL_POLICY_INVALID" in finding_codes(tmp_path, tracked)


def test_active_skill_rejects_stale_evaluation_result(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    result_text = result_path.read_text(encoding="utf-8")
    current_sha_line = next(
        line for line in result_text.splitlines() if line.startswith("skill_content_sha256 = ")
    )
    stale_sha = "0" * 64
    result_path.write_text(
        result_text.replace(
            current_sha_line,
            f'skill_content_sha256 = "{stale_sha}"',
        ),
        encoding="utf-8",
    )
    assert "SKILL_EVAL_RESULT_STALE" in finding_codes(tmp_path, tracked)


def test_active_skill_rejects_stale_eval_definition(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    eval_path = tmp_path / ".agents/skill-evals/audit.toml"
    eval_path.write_text(
        eval_path.read_text(encoding="utf-8") + "\n# changed after evaluation\n",
        encoding="utf-8",
    )
    assert "SKILL_EVAL_RESULT_STALE" in finding_codes(tmp_path, tracked)


def test_active_skill_rejects_nonexistent_evaluated_commit(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    payload = result_path.read_text(encoding="utf-8")
    commit_line = next(
        line for line in payload.splitlines() if line.startswith("evaluated_commit_sha = ")
    )
    result_path.write_text(
        payload.replace(commit_line, f'evaluated_commit_sha = "{"0" * 40}"'),
        encoding="utf-8",
    )
    assert "SKILL_EVAL_RESULT_COMMIT" in finding_codes(tmp_path, tracked)


def test_active_skill_rejects_bytes_not_present_in_evaluated_commit(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    skill_path = tmp_path / ".agents/skills/audit/SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nNew evaluated instruction.\n",
        encoding="utf-8",
    )
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    payload = result_path.read_text(encoding="utf-8")
    sha_line = next(
        line for line in payload.splitlines() if line.startswith("skill_content_sha256 = ")
    )
    current_sha = RepositoryValidator._canonical_text_sha256(skill_path)
    result_path.write_text(
        payload.replace(sha_line, f'skill_content_sha256 = "{current_sha}"'),
        encoding="utf-8",
    )
    codes = finding_codes(tmp_path, tracked)
    assert "SKILL_EVAL_RESULT_STALE" not in codes
    assert "SKILL_EVAL_RESULT_COMMIT" in codes


def test_active_skill_rejects_failed_score_and_p1(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    payload = result_path.read_text(encoding="utf-8")
    payload = payload.replace("total_score = 10", "total_score = 9", 1)
    payload = payload.replace("p1_count = 0", "p1_count = 1", 1)
    result_path.write_text(payload, encoding="utf-8")
    codes = finding_codes(tmp_path, tracked)
    assert "SKILL_EVAL_RESULT_SCORE" in codes
    assert "SKILL_EVAL_RESULT_FINDINGS" in codes


def test_active_skill_rejects_forbidden_case_result(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    result_path.write_text(
        result_path.read_text(encoding="utf-8").replace(
            "forbidden_observed = false",
            "forbidden_observed = true",
            1,
        ),
        encoding="utf-8",
    )
    assert "SKILL_EVAL_RESULT_SCORE" in finding_codes(tmp_path, tracked)


def test_active_skill_requires_matching_owner_review(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    result_path.write_text(
        result_path.read_text(encoding="utf-8").replace(
            'owner = "Test"\napproved = true', 'owner = "Other"\napproved = false'
        ),
        encoding="utf-8",
    )
    assert "SKILL_EVAL_OWNER_REVIEW" in finding_codes(tmp_path, tracked)


def test_active_skill_result_binds_side_effect_boundary(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    result_path = tmp_path / ".agents/skill-eval-results/audit.toml"
    result_path.write_text(
        result_path.read_text(encoding="utf-8").replace(
            'side_effect_policy = "read-only"',
            'side_effect_policy = "instruction-only"',
        ),
        encoding="utf-8",
    )
    assert "SKILL_EVAL_RESULT_INVALID" in finding_codes(tmp_path, tracked)


def test_active_skill_requires_evaluated_artifact(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    (tmp_path / "docs/test.md").unlink()
    assert "SKILL_EVAL_ARTIFACT_MISSING" in finding_codes(tmp_path, tracked)


def test_unpinned_action_fails(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n",
    )
    tracked.add(".github/workflows/ci.yml")
    assert "ACTION_UNPINNED" in finding_codes(tmp_path, tracked)


def test_runtime_cli_contract_is_required_when_pyproject_exists(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    write(
        tmp_path,
        "pyproject.toml",
        """[project]
name = "test"
requires-python = ">=3.12,<3.14"
""",
    )
    tracked.add("pyproject.toml")
    codes = finding_codes(tmp_path, tracked)
    assert "CLI_ENTRYPOINT_INVALID" in codes
    assert "RUNTIME_PATH_MISSING" in codes
    assert "CI_COMMAND_MISSING" in codes


def test_synthetic_media_is_allowed_only_in_test_fixtures(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    fixture = "tests/fixtures/synthetic-media.mp4"
    write(tmp_path, fixture, "synthetic bytes only")
    tracked.add(fixture)
    assert "FORBIDDEN_ARTIFACT" not in finding_codes(tmp_path, tracked)

    outside = "tests/synthetic-media.mp4"
    write(tmp_path, outside, "synthetic bytes only")
    tracked.add(outside)
    assert "FORBIDDEN_ARTIFACT" in finding_codes(tmp_path, tracked)
