from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


def document(doc_id: str, canonical_for: str) -> str:
    return f"""---
schema_version: "1.0"
document_id: {doc_id}
title: Test
document_type: governance
status: active
canonical: true
canonical_for: {canonical_for}
created_at: 2026-01-01T00:00:00+09:00
updated_at: 2026-01-01T00:00:00+09:00
authors: [Test]
owners: [Test]
reviewers: []
source_issue: 1
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: []
---
# Test
"""


def make_valid_repo(root: Path) -> set[str]:
    adapters = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md"]
    for adapter in adapters:
        write(
            root,
            adapter,
            document("DOC-" + adapter.replace("/", "-").replace(".", "-").upper(), adapter)
            + "\nCANONICAL_AGENT_POLICY: `.agents/policy.md`\nSKILL_REGISTRY: `.agents/skill-system.toml`\n",
        )
    write(root, "docs/test.md", document("DOC-TEST", "test"))
    entries = []
    for adapter in adapters:
        did = "DOC-" + adapter.replace("/", "-").replace(".", "-").upper()
        entries.append(
            {
                "document_id": did,
                "path": adapter,
                "document_type": "governance",
                "status": "active",
                "canonical": True,
                "canonical_for": adapter,
                "owners": ["Test"],
            }
        )
    entries.append(
        {
            "document_id": "DOC-TEST",
            "path": "docs/test.md",
            "document_type": "governance",
            "status": "active",
            "canonical": True,
            "canonical_for": "test",
            "owners": ["Test"],
        }
    )
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
required_eval_cases = 2
[[skills]]
id = "audit"
path = ".agents/skills/audit/SKILL.md"
lifecycle = "active"
automatic_trigger = false
side_effect_policy = "read-only"
eval_path = ".agents/skill-evals/audit.toml"
owner = "Test"
""",
    )
    write(
        root, ".agents/skills/audit/SKILL.md", "---\nname: audit\ndescription: test\n---\n# audit\n"
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
[[cases]]
id = "b"
prompt = "b"
expected = ["b"]
forbidden = []
""",
    )
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_valid_fixture_passes(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    assert RepositoryValidator(tmp_path, tracked).validate() == []


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
    write(tmp_path, "docs/other.md", document("DOC-OTHER", "test") + f"\n{secret}\n")
    tracked.add("docs/other.md")
    findings = RepositoryValidator(tmp_path, tracked).validate()
    codes = {finding.code for finding in findings}
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
    codes = {finding.code for finding in RepositoryValidator(tmp_path, tracked).validate()}
    assert "DRAFT_SKILL_TRIGGER" in codes
    assert "DRAFT_SKILL_SIDE_EFFECT" in codes


def test_unpinned_action_fails(tmp_path: Path) -> None:
    tracked = make_valid_repo(tmp_path)
    write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n",
    )
    tracked.add(".github/workflows/ci.yml")
    codes = {finding.code for finding in RepositoryValidator(tmp_path, tracked).validate()}
    assert "ACTION_UNPINNED" in codes
