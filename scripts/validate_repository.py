from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
ACTION_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_REGISTRY_FIELDS = {
    "document_id",
    "path",
    "document_type",
    "status",
    "canonical",
    "canonical_for",
    "owners",
    "metadata_mode",
}
REQUIRED_FRONT_MATTER_FIELDS = {
    "schema_version",
    "document_id",
    "title",
    "document_type",
    "status",
    "canonical",
    "canonical_for",
    "owners",
    "supersedes",
    "superseded_by",
    "related_documents",
}
REGISTRY_MATCH_FIELDS = {
    "document_id",
    "document_type",
    "status",
    "canonical",
    "canonical_for",
    "owners",
}
ALLOWED_METADATA_MODES = {"front-matter", "registry-only", "non-markdown"}
ALLOWED_LIFECYCLES = {"observed", "candidate", "draft", "active", "deprecated", "rejected"}
ALLOWED_SIDE_EFFECTS = {
    "read-only",
    "workspace-write",
    "repository-write",
    "external-write",
    "instruction-only",
}
ADAPTERS = {
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
}
ADAPTER_MARKERS = (
    "CANONICAL_AGENT_POLICY:",
    "SKILL_REGISTRY:",
)
FORBIDDEN_SUFFIXES = {
    ".mp4",
    ".m4v",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".ts",
    ".m3u8",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".srt",
    ".vtt",
    ".ttml",
    ".ass",
    ".ssa",
    ".pdf",
    ".docx",
    ".pptx",
}
FORBIDDEN_PREFIXES = (
    ".local/",
    "jobs/",
    "work/",
    "artifacts/",
    "approval/",
    "authoring/",
    "data/raw/",
    "data/derived/",
    "exports/",
    "human-review/",
    "manifests/",
    "output/",
)
SECRET_PATTERNS = {
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "openai-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "authorization": re.compile(
        r"Authorization:\s*(?:Bearer|Basic)\s+[^\s]{12,}",
        re.IGNORECASE,
    ),
    "signed-query": re.compile(
        r"[?&](?:token|access_token|sig|signature|x-amz-signature|"
        r"x-amz-security-token|x-amz-credential|policy|key-pair-id)=[^&\s]{16,}",
        re.IGNORECASE,
    ),
}
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml", ".json", ".jsonl", ".txt"}
RUNTIME_REQUIRED_PATHS = {
    "src/video_evidence_pipeline/__init__.py",
    "src/video_evidence_pipeline/cli.py",
    "src/video_evidence_pipeline/domain/job.py",
    "src/video_evidence_pipeline/acquisition/local_file.py",
    "src/video_evidence_pipeline/storage/layout.py",
    "src/video_evidence_pipeline/schemas/generation.py",
}
CI_REQUIRED_COMMANDS = {
    "uv run ruff check src scripts tests",
    "uv run ruff format --check src scripts tests",
    "uv run pytest -q tests/test_full_e2e.py",
    "uv run pytest -q",
    "uv run vep schema check",
    "uv run python scripts/validate_repository.py --root .",
}


@dataclass(frozen=True, order=True)
class Finding:
    code: str
    path: str
    message: str


class RepositoryValidator:
    def __init__(self, root: Path, tracked_files: set[str] | None = None) -> None:
        self.root = root.resolve()
        self.tracked_files = tracked_files or self._discover_tracked_files()
        self.findings: list[Finding] = []

    def add(self, code: str, path: str | Path, message: str) -> None:
        self.findings.append(Finding(code, str(path).replace("\\", "/"), message))

    def _discover_tracked_files(self) -> set[str]:
        try:
            result = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=self.root,
                check=True,
                capture_output=True,
            )
            return {p.decode("utf-8") for p in result.stdout.split(b"\0") if p}
        except (subprocess.CalledProcessError, FileNotFoundError):
            ignored = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
            return {
                p.relative_to(self.root).as_posix()
                for p in self.root.rglob("*")
                if p.is_file() and not any(part in ignored for part in p.parts)
            }

    def validate(self) -> list[Finding]:
        self._validate_syntax()
        self._validate_document_registry()
        self._validate_skill_system()
        self._validate_adapters()
        self._validate_links()
        self._validate_workflows()
        self._validate_runtime_contracts()
        self._validate_forbidden_artifacts()
        self._validate_secrets()
        return sorted(set(self.findings))

    def _load_yaml(self, relative: str) -> Any:
        path = self.root / relative
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            self.add("YAML_INVALID", relative, str(exc))
            return None

    def _read_text(self, relative: str) -> str | None:
        try:
            return (self.root / relative).read_text(encoding="utf-8")
        except OSError as exc:
            self.add("DOCUMENT_READ", relative, str(exc))
            return None

    def _parse_front_matter(self, relative: str) -> dict[str, Any] | None:
        text = self._read_text(relative)
        if text is None:
            return None
        match = FRONT_MATTER_RE.match(text)
        if not match:
            self.add(
                "FRONT_MATTER_MISSING",
                relative,
                "metadata_mode=front-matter requires YAML front matter",
            )
            return None
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            self.add("FRONT_MATTER_INVALID", relative, str(exc))
            return None
        if not isinstance(data, dict):
            self.add("FRONT_MATTER_INVALID", relative, "front matter must be a mapping")
            return None
        return data

    def _validate_syntax(self) -> None:
        for relative in sorted(self.tracked_files):
            path = self.root / relative
            if not path.is_file():
                continue
            if path.suffix in {".yaml", ".yml"}:
                try:
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError) as exc:
                    self.add("YAML_INVALID", relative, str(exc))
            elif path.suffix == ".toml" or path.name == "pyproject.toml":
                try:
                    tomllib.loads(path.read_text(encoding="utf-8"))
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    self.add("TOML_INVALID", relative, str(exc))

    def _validate_document_registry(self) -> None:
        registry_path = "docs/registry/documents.yaml"
        registry = self._load_yaml(registry_path)
        if not isinstance(registry, dict):
            return
        docs = registry.get("documents")
        if not isinstance(docs, list):
            self.add("REGISTRY_INVALID", registry_path, "documents must be a list")
            return

        ids: dict[str, str] = {}
        canonical_for: dict[str, str] = {}
        entries: dict[str, dict[str, Any]] = {}

        for index, entry in enumerate(docs):
            if not isinstance(entry, dict):
                self.add(
                    "REGISTRY_ENTRY_INVALID",
                    registry_path,
                    f"entry {index} must be a mapping",
                )
                continue

            missing_registry_fields = REQUIRED_REGISTRY_FIELDS - set(entry)
            for field in sorted(missing_registry_fields):
                self.add("REGISTRY_FIELD_MISSING", registry_path, f"entry {index}: {field}")

            doc_id = entry.get("document_id")
            relative = entry.get("path")
            if not isinstance(doc_id, str) or not isinstance(relative, str):
                self.add(
                    "REGISTRY_ENTRY_INVALID",
                    registry_path,
                    f"entry {index} lacks document_id/path",
                )
                continue

            if doc_id in ids:
                self.add(
                    "DOCUMENT_ID_DUPLICATE",
                    registry_path,
                    f"{doc_id}: {ids[doc_id]} and {relative}",
                )
            ids[doc_id] = relative
            entries[doc_id] = entry

            cfor = entry.get("canonical_for")
            if entry.get("canonical") is True:
                if not isinstance(cfor, str) or not cfor:
                    self.add(
                        "CANONICAL_FOR_MISSING",
                        relative,
                        "canonical document requires canonical_for",
                    )
                elif cfor in canonical_for:
                    self.add(
                        "CANONICAL_FOR_DUPLICATE",
                        relative,
                        f"{cfor}: also {canonical_for[cfor]}",
                    )
                else:
                    canonical_for[cfor] = relative

            if relative not in self.tracked_files or not (self.root / relative).is_file():
                self.add("REGISTERED_PATH_MISSING", relative, f"registered by {doc_id}")
                continue

            mode = entry.get("metadata_mode")
            if mode not in ALLOWED_METADATA_MODES:
                self.add("METADATA_MODE_INVALID", relative, repr(mode))
                continue

            if mode == "front-matter":
                self._validate_front_matter_document(relative, entry)
            elif mode == "registry-only":
                self._validate_registry_only_document(relative)
            else:
                self._validate_non_markdown_document(relative)

        valid_ids = set(entries)
        for doc_id, entry in entries.items():
            if entry.get("metadata_mode") != "front-matter":
                continue
            relative = entry.get("path")
            if not isinstance(relative, str) or not (self.root / relative).is_file():
                continue
            front = self._parse_front_matter(relative)
            if front is None:
                continue
            refs: list[str] = []
            refs.extend(front.get("supersedes") or [])
            refs.extend(front.get("related_documents") or [])
            if front.get("superseded_by"):
                refs.append(front["superseded_by"])
            for ref in refs:
                if ref not in valid_ids:
                    self.add("DOCUMENT_REFERENCE_UNKNOWN", relative, f"{doc_id} references {ref}")

    def _validate_front_matter_document(
        self,
        relative: str,
        entry: dict[str, Any],
    ) -> None:
        if not relative.endswith(".md"):
            self.add(
                "METADATA_MODE_PATH_MISMATCH",
                relative,
                "front-matter mode requires a Markdown path",
            )
            return

        front = self._parse_front_matter(relative)
        if front is None:
            return

        missing = REQUIRED_FRONT_MATTER_FIELDS - set(front)
        for field in sorted(missing):
            self.add("DOCUMENT_FIELD_MISSING", relative, field)

        for field in REGISTRY_MATCH_FIELDS:
            if field in front and front.get(field) != entry.get(field):
                self.add(
                    "REGISTRY_FRONT_MATTER_MISMATCH",
                    relative,
                    f"{field}: registry={entry.get(field)!r}, front={front.get(field)!r}",
                )

        created = front.get("created_at")
        updated = front.get("updated_at")
        if created is not None and updated is not None:
            try:
                if created > updated:
                    self.add("DOCUMENT_TIME_ORDER", relative, "created_at is after updated_at")
            except TypeError:
                self.add(
                    "DOCUMENT_TIME_INVALID",
                    relative,
                    "created_at and updated_at are not comparable",
                )

        if front.get("status") == "historical":
            if front.get("canonical") is not False:
                self.add(
                    "HISTORICAL_CANONICAL",
                    relative,
                    "historical document must not be canonical",
                )
            if front.get("canonical_for") is not None:
                self.add(
                    "HISTORICAL_CANONICAL_FOR",
                    relative,
                    "historical document requires canonical_for: null",
                )
            for field in ("historical_at", "historical_reason"):
                if field not in front:
                    self.add("HISTORICAL_FIELD_MISSING", relative, field)
            if "superseded_by" not in front:
                self.add("HISTORICAL_FIELD_MISSING", relative, "superseded_by")

    def _validate_registry_only_document(self, relative: str) -> None:
        if not relative.endswith(".md"):
            self.add(
                "METADATA_MODE_PATH_MISMATCH",
                relative,
                "registry-only mode requires a Markdown path",
            )
            return
        text = self._read_text(relative)
        if text is not None and FRONT_MATTER_RE.match(text):
            self.add(
                "REGISTRY_ONLY_FRONT_MATTER",
                relative,
                "registry-only document must begin with its usable content",
            )

    def _validate_non_markdown_document(self, relative: str) -> None:
        if relative.endswith(".md"):
            self.add(
                "METADATA_MODE_PATH_MISMATCH",
                relative,
                "non-markdown mode cannot be used for a Markdown path",
            )

    def _validate_skill_system(self) -> None:
        relative = ".agents/skill-system.toml"
        path = self.root / relative
        if relative not in self.tracked_files or not path.is_file():
            self.add("SKILL_SYSTEM_MISSING", relative, "required Skill registry is missing")
            return
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return
        policy = data.get("policy") or {}
        required_cases = policy.get("required_eval_cases")
        promotion_threshold = policy.get("promotion_threshold")
        if policy.get("activation_mode") != "explicit-only":
            self.add(
                "SKILL_POLICY_INVALID",
                relative,
                "activation_mode must remain explicit-only",
            )
        if (
            not isinstance(required_cases, int)
            or isinstance(required_cases, bool)
            or required_cases < 2
        ):
            self.add(
                "SKILL_POLICY_INVALID",
                relative,
                "required_eval_cases must be an integer of at least 2",
            )
            required_cases = 2
        if (
            not isinstance(promotion_threshold, int)
            or isinstance(promotion_threshold, bool)
            or promotion_threshold < 9
        ):
            self.add(
                "SKILL_POLICY_INVALID",
                relative,
                "promotion_threshold must be an integer of at least 9",
            )
            promotion_threshold = 9
        skills = data.get("skills")
        if not isinstance(skills, list):
            self.add("SKILL_SYSTEM_INVALID", relative, "skills must be an array of tables")
            return
        ids: set[str] = set()
        paths: set[str] = set()
        result_paths: set[str] = set()
        for entry in skills:
            if not isinstance(entry, dict):
                self.add("SKILL_ENTRY_INVALID", relative, "skill entry must be a mapping")
                continue
            sid = entry.get("id")
            skill_path = entry.get("path")
            lifecycle = entry.get("lifecycle")
            side_effect = entry.get("side_effect_policy")
            eval_path = entry.get("eval_path")
            if not all(
                isinstance(x, str) for x in (sid, skill_path, lifecycle, side_effect, eval_path)
            ):
                self.add("SKILL_ENTRY_INVALID", relative, f"invalid required fields: {entry!r}")
                continue
            if sid in ids:
                self.add("SKILL_ID_DUPLICATE", relative, sid)
            ids.add(sid)
            if skill_path in paths:
                self.add("SKILL_PATH_DUPLICATE", skill_path, sid)
            paths.add(skill_path)
            if lifecycle not in ALLOWED_LIFECYCLES:
                self.add("SKILL_LIFECYCLE_INVALID", skill_path, lifecycle)
            if side_effect not in ALLOWED_SIDE_EFFECTS:
                self.add("SKILL_SIDE_EFFECT_INVALID", skill_path, side_effect)
            if entry.get("automatic_trigger") is not False:
                self.add(
                    "SKILL_TRIGGER_INVALID",
                    skill_path,
                    "explicit-only Skill must not auto-trigger",
                )
            if lifecycle == "draft":
                if entry.get("automatic_trigger") is not False:
                    self.add("DRAFT_SKILL_TRIGGER", skill_path, "draft Skill must not auto-trigger")
                if side_effect != "instruction-only":
                    self.add(
                        "DRAFT_SKILL_SIDE_EFFECT",
                        skill_path,
                        "draft Skill must be instruction-only",
                    )
            if skill_path not in self.tracked_files or not (self.root / skill_path).is_file():
                self.add("SKILL_PATH_MISSING", skill_path, sid)
                continue
            front = self._parse_skill_front_matter(skill_path)
            if front is not None and front.get("name") != sid:
                self.add("SKILL_NAME_MISMATCH", skill_path, f"expected {sid!r}")
            if eval_path not in self.tracked_files or not (self.root / eval_path).is_file():
                self.add("SKILL_EVAL_MISSING", eval_path, sid)
                continue
            try:
                evaluation = tomllib.loads((self.root / eval_path).read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if evaluation.get("skill_id") != sid:
                self.add("SKILL_EVAL_ID_MISMATCH", eval_path, f"expected {sid!r}")
            cases = evaluation.get("cases") or []
            if not isinstance(cases, list) or len(cases) < int(required_cases):
                self.add("SKILL_EVAL_CASES", eval_path, f"requires at least {required_cases} cases")
            if lifecycle == "active":
                result_path = entry.get("eval_result_path")
                if not isinstance(result_path, str) or not result_path:
                    self.add(
                        "ACTIVE_SKILL_RESULT_MISSING",
                        skill_path,
                        "active Skill requires eval_result_path",
                    )
                    continue
                if result_path in result_paths:
                    self.add("SKILL_RESULT_PATH_DUPLICATE", result_path, sid)
                result_paths.add(result_path)
                self._validate_skill_result(
                    sid=sid,
                    owner=entry.get("owner"),
                    side_effect_policy=side_effect,
                    skill_path=skill_path,
                    eval_path=eval_path,
                    result_path=result_path,
                    evaluation=evaluation,
                    promotion_threshold=promotion_threshold,
                )

    def _validate_skill_result(
        self,
        *,
        sid: str,
        owner: object,
        side_effect_policy: str,
        skill_path: str,
        eval_path: str,
        result_path: str,
        evaluation: dict[str, Any],
        promotion_threshold: object,
    ) -> None:
        path = self.root / result_path
        if result_path not in self.tracked_files or not path.is_file():
            self.add("ACTIVE_SKILL_RESULT_MISSING", result_path, sid)
            return
        try:
            result = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            self.add("SKILL_EVAL_RESULT_INVALID", result_path, str(exc))
            return

        required_strings = (
            "skill_id",
            "skill_path",
            "skill_content_sha256",
            "eval_definition_path",
            "eval_definition_sha256",
            "evaluated_commit_sha",
            "evaluated_at",
            "evaluator_identity",
            "evaluator_method",
            "finding_summary",
            "side_effect_policy",
        )
        if result.get("schema_version") != "1.0" or not all(
            isinstance(result.get(key), str) and result[key].strip() for key in required_strings
        ):
            self.add(
                "SKILL_EVAL_RESULT_INVALID",
                result_path,
                "schema and required string fields are invalid",
            )
            return
        if (
            result["skill_id"] != sid
            or result["skill_path"] != skill_path
            or result["eval_definition_path"] != eval_path
            or result["side_effect_policy"] != side_effect_policy
        ):
            self.add(
                "SKILL_EVAL_RESULT_INVALID",
                result_path,
                "Skill ID, definition paths, or side-effect policy do not match the registry",
            )
        current_skill_sha = self._canonical_text_sha256(self.root / skill_path)
        current_eval_sha = self._canonical_text_sha256(self.root / eval_path)
        if (
            not SHA256_RE.fullmatch(result["skill_content_sha256"])
            or not SHA256_RE.fullmatch(result["eval_definition_sha256"])
            or result["skill_content_sha256"] != current_skill_sha
            or result["eval_definition_sha256"] != current_eval_sha
        ):
            self.add(
                "SKILL_EVAL_RESULT_STALE",
                result_path,
                "evaluation evidence does not match current Skill or eval bytes",
            )
        evaluated_commit_sha = result["evaluated_commit_sha"]
        if not SHA40_RE.fullmatch(evaluated_commit_sha):
            self.add(
                "SKILL_EVAL_RESULT_INVALID",
                result_path,
                "evaluated_commit_sha must be a lowercase 40-character SHA",
            )
        elif not self._evaluation_commit_matches(
            evaluated_commit_sha,
            {
                skill_path: result["skill_content_sha256"],
                eval_path: result["eval_definition_sha256"],
            },
        ):
            self.add(
                "SKILL_EVAL_RESULT_COMMIT",
                result_path,
                "evaluated commit is not an ancestor or does not contain the recorded bytes",
            )
        for field in ("evaluated_at",):
            try:
                parsed = datetime.fromisoformat(result[field].replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is None or parsed.tzinfo is None:
                self.add(
                    "SKILL_EVAL_RESULT_INVALID",
                    result_path,
                    f"{field} must be timezone-aware",
                )

        case_definitions = evaluation.get("cases")
        if not isinstance(case_definitions, list):
            self.add("SKILL_EVAL_RESULT_INVALID", result_path, "eval cases are invalid")
            return
        definitions: dict[str, list[str]] = {}
        for case in case_definitions:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                self.add("SKILL_EVAL_RESULT_INVALID", result_path, "eval case is invalid")
                return
            criteria = case.get("criteria")
            if not isinstance(criteria, list) or len(criteria) != 5:
                self.add(
                    "SKILL_EVAL_RESULT_INVALID",
                    eval_path,
                    f"case {case['id']!r} requires exactly five criteria",
                )
                return
            criterion_ids = [
                item.get("id")
                for item in criteria
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("description"), str)
                and item["description"].strip()
            ]
            if len(criterion_ids) != 5 or len(set(criterion_ids)) != 5:
                self.add(
                    "SKILL_EVAL_RESULT_INVALID",
                    eval_path,
                    f"case {case['id']!r} criterion IDs are invalid",
                )
                return
            if case["id"] in definitions:
                self.add("SKILL_EVAL_RESULT_INVALID", eval_path, "duplicate case ID")
                return
            definitions[case["id"]] = criterion_ids

        case_results = result.get("case_results")
        if not isinstance(case_results, list) or len(case_results) != len(definitions):
            self.add(
                "SKILL_EVAL_RESULT_INVALID",
                result_path,
                "case result set does not match the eval definition",
            )
            return
        result_map = {
            item.get("case_id"): item
            for item in case_results
            if isinstance(item, dict) and isinstance(item.get("case_id"), str)
        }
        if set(result_map) != set(definitions) or len(result_map) != len(case_results):
            self.add(
                "SKILL_EVAL_RESULT_INVALID",
                result_path,
                "case result IDs do not match the eval definition",
            )
            return

        computed_total = 0
        computed_maximum = 0
        all_cases_pass = True
        for case_id, criterion_ids in definitions.items():
            case = result_map[case_id]
            criterion_results = case.get("criteria")
            if not isinstance(criterion_results, list):
                self.add(
                    "SKILL_EVAL_RESULT_INVALID", result_path, f"case {case_id!r} has no criteria"
                )
                return
            criterion_map = {
                item.get("id"): item
                for item in criterion_results
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if set(criterion_map) != set(criterion_ids) or len(criterion_map) != len(
                criterion_results
            ):
                self.add(
                    "SKILL_EVAL_RESULT_INVALID",
                    result_path,
                    f"case {case_id!r} criterion results are incomplete",
                )
                return
            passed = sum(
                item.get("passed") is True
                and isinstance(item.get("evidence"), str)
                and bool(item["evidence"].strip())
                for item in criterion_map.values()
            )
            maximum = len(criterion_ids)
            evidence = case.get("evidence")
            case_pass = (
                case.get("result") == "pass"
                and case.get("forbidden_observed") is False
                and passed == maximum
                and isinstance(evidence, list)
                and bool(evidence)
                and all(isinstance(item, str) and item.strip() for item in evidence)
            )
            if case.get("score") != passed or case.get("maximum_score") != maximum:
                case_pass = False
            computed_total += passed
            computed_maximum += maximum
            all_cases_pass = all_cases_pass and case_pass

        try:
            expected_threshold = max(
                int(promotion_threshold), int(evaluation.get("minimum_score", 0))
            )
        except (TypeError, ValueError):
            expected_threshold = -1
        if (
            result.get("total_score") != computed_total
            or result.get("maximum_score") != computed_maximum
            or result.get("threshold") != expected_threshold
            or computed_total < expected_threshold
            or not all_cases_pass
        ):
            self.add(
                "SKILL_EVAL_RESULT_SCORE",
                result_path,
                "scores or case pass status do not satisfy the objective rubric",
            )
        if (
            result.get("p0_count") != 0
            or result.get("p1_count") != 0
            or result.get("promotion_eligible") is not True
        ):
            self.add(
                "SKILL_EVAL_RESULT_FINDINGS",
                result_path,
                "active Skill requires P0=0, P1=0, and promotion eligibility",
            )

        artifacts = result.get("required_artifacts")
        if (
            not isinstance(artifacts, list)
            or not artifacts
            or len(artifacts) != len(set(artifacts))
        ):
            self.add(
                "SKILL_EVAL_ARTIFACT_MISSING",
                result_path,
                "required_artifacts must be non-empty and unique",
            )
        else:
            for artifact in artifacts:
                if (
                    not isinstance(artifact, str)
                    or artifact.startswith(("/", "\\"))
                    or ".." in Path(artifact).parts
                    or artifact not in self.tracked_files
                    or not (self.root / artifact).is_file()
                ):
                    self.add("SKILL_EVAL_ARTIFACT_MISSING", result_path, str(artifact))
            if SHA40_RE.fullmatch(evaluated_commit_sha) and not self._evaluation_commit_matches(
                evaluated_commit_sha,
                {artifact: None for artifact in artifacts if isinstance(artifact, str)},
            ):
                self.add(
                    "SKILL_EVAL_RESULT_COMMIT",
                    result_path,
                    "required artifacts were absent from the evaluated commit",
                )

        review = result.get("owner_review")
        if (
            not isinstance(owner, str)
            or not isinstance(review, dict)
            or review.get("owner") != owner
            or review.get("approved") is not True
            or not isinstance(review.get("method"), str)
            or not review["method"].strip()
            or not isinstance(review.get("reviewed_at"), str)
        ):
            self.add(
                "SKILL_EVAL_OWNER_REVIEW",
                result_path,
                "owner review is absent or does not match the registry owner",
            )
        else:
            try:
                reviewed = datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00"))
            except ValueError:
                reviewed = None
            if reviewed is None or reviewed.tzinfo is None:
                self.add(
                    "SKILL_EVAL_OWNER_REVIEW",
                    result_path,
                    "owner review timestamp must be timezone-aware",
                )

    def _evaluation_commit_matches(
        self,
        commit_sha: str,
        expected_files: dict[str, str | None],
    ) -> bool:
        try:
            repository_root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if Path(repository_root).resolve() != self.root:
                return False
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit_sha, "HEAD"],
                cwd=self.root,
                check=True,
                capture_output=True,
            )
            for relative, expected_sha256 in expected_files.items():
                blob = subprocess.run(
                    ["git", "show", f"{commit_sha}:{relative}"],
                    cwd=self.root,
                    check=True,
                    capture_output=True,
                ).stdout
                if (
                    expected_sha256 is not None
                    and hashlib.sha256(blob).hexdigest() != expected_sha256
                ):
                    return False
        except (OSError, subprocess.CalledProcessError):
            return False
        return True

    @staticmethod
    def _canonical_text_sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    def _parse_skill_front_matter(self, relative: str) -> dict[str, Any] | None:
        text = (self.root / relative).read_text(encoding="utf-8")
        match = FRONT_MATTER_RE.match(text)
        if not match:
            self.add("SKILL_FRONT_MATTER_MISSING", relative, "SKILL.md needs name/description")
            return None
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            self.add("SKILL_FRONT_MATTER_INVALID", relative, str(exc))
            return None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("name"), str)
            or not isinstance(data.get("description"), str)
        ):
            self.add("SKILL_FRONT_MATTER_INVALID", relative, "name and description are required")
            return None
        return data

    def _validate_adapters(self) -> None:
        for relative in sorted(ADAPTERS):
            path = self.root / relative
            if relative not in self.tracked_files or not path.is_file():
                self.add("AGENT_ADAPTER_MISSING", relative, "required adapter is missing")
                continue
            text = path.read_text(encoding="utf-8")
            for marker in ADAPTER_MARKERS:
                if marker not in text:
                    self.add("AGENT_ADAPTER_MARKER", relative, f"missing {marker}")

    def _validate_links(self) -> None:
        for relative in sorted(self.tracked_files):
            if not relative.endswith(".md"):
                continue
            path = self.root / relative
            if not path.is_file():
                continue
            text = FENCED_CODE_RE.sub("", path.read_text(encoding="utf-8"))
            for target in MARKDOWN_LINK_RE.findall(text):
                target = target.strip().split()[0].strip("<>")
                if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                file_target = target.split("#", 1)[0]
                if not file_target:
                    continue
                resolved = (path.parent / file_target).resolve()
                try:
                    resolved.relative_to(self.root)
                except ValueError:
                    self.add("LINK_OUTSIDE_REPOSITORY", relative, target)
                    continue
                if not resolved.exists():
                    self.add("LINK_TARGET_MISSING", relative, target)

    def _validate_workflows(self) -> None:
        for relative in sorted(self.tracked_files):
            if not relative.startswith(".github/workflows/") or not relative.endswith(
                (".yml", ".yaml")
            ):
                continue
            text = (self.root / relative).read_text(encoding="utf-8")
            for use in ACTION_RE.findall(text):
                if use.startswith("./"):
                    continue
                if "@" not in use:
                    self.add("ACTION_UNPINNED", relative, use)
                    continue
                ref = use.rsplit("@", 1)[1]
                if not SHA40_RE.fullmatch(ref):
                    self.add("ACTION_UNPINNED", relative, use)

    def _validate_runtime_contracts(self) -> None:
        pyproject_path = self.root / "pyproject.toml"
        if not pyproject_path.is_file():
            return
        try:
            pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return
        project = pyproject.get("project") or {}
        scripts = project.get("scripts") or {}
        if scripts.get("vep") != "video_evidence_pipeline.cli:app":
            self.add(
                "CLI_ENTRYPOINT_INVALID",
                "pyproject.toml",
                "project.scripts.vep must point to video_evidence_pipeline.cli:app",
            )
        if project.get("requires-python") != ">=3.12,<3.14":
            self.add("PYTHON_BASELINE_INVALID", "pyproject.toml", "Python 3.12 baseline changed")
        for relative in sorted(RUNTIME_REQUIRED_PATHS):
            if not (self.root / relative).is_file():
                self.add("RUNTIME_PATH_MISSING", relative, "required package contract is missing")

        workflow_path = self.root / ".github/workflows/ci.yml"
        try:
            workflow = workflow_path.read_text(encoding="utf-8")
        except OSError:
            workflow = ""
        for command in sorted(CI_REQUIRED_COMMANDS):
            if command not in workflow:
                self.add("CI_COMMAND_MISSING", ".github/workflows/ci.yml", command)

        source_root = str(self.root / "src")
        inserted = source_root not in sys.path
        if inserted:
            sys.path.insert(0, source_root)
        try:
            from video_evidence_pipeline.schemas.generation import generated_schemas

            generated = generated_schemas()
            for filename, expected in generated.items():
                path = self.root / "schemas" / filename
                if not path.is_file():
                    self.add(
                        "SCHEMA_MISSING", path.relative_to(self.root), "generated Schema is missing"
                    )
                elif path.read_bytes() != expected:
                    self.add(
                        "SCHEMA_DRIFT", path.relative_to(self.root), "generated Schema is stale"
                    )
            for path in sorted((self.root / "schemas").glob("*.schema.json")):
                if path.name not in generated:
                    self.add(
                        "SCHEMA_DRIFT",
                        path.relative_to(self.root),
                        "unexpected generated Schema is committed",
                    )
        except Exception as exc:
            self.add("SCHEMA_GENERATION_FAILED", "schemas", type(exc).__name__)
        finally:
            if inserted:
                sys.path.remove(source_root)

    def _validate_forbidden_artifacts(self) -> None:
        for relative in sorted(self.tracked_files):
            lower = relative.lower()
            if any(lower.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
                self.add("FORBIDDEN_PATH", relative, "local or derived artifact path")
            suffix = Path(lower).suffix
            if suffix in FORBIDDEN_SUFFIXES and not lower.startswith("tests/fixtures/"):
                self.add("FORBIDDEN_ARTIFACT", relative, suffix)

    def _validate_secrets(self) -> None:
        for relative in sorted(self.tracked_files):
            path = self.root / relative
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for name, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    self.add("SECRET_PATTERN", relative, name)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate repository governance and safety contracts"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    findings = RepositoryValidator(args.root).validate()
    if findings:
        for finding in findings:
            print(f"ERROR [{finding.code}] {finding.path}: {finding.message}")
        print(f"repository validation failed: {len(findings)} finding(s)")
        return 1
    print("repository validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
