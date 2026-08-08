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
[[cases]]
id = "b"
prompt = "b"
expected = ["b"]
forbidden = []
""",
    )
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


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
