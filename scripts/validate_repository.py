from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
ACTION_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

REQUIRED_DOCUMENT_FIELDS = {
    "schema_version",
    "document_id",
    "title",
    "document_type",
    "status",
    "canonical",
    "canonical_for",
    "created_at",
    "updated_at",
    "authors",
    "owners",
    "reviewers",
    "source_issue",
    "change_control",
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
    "data/raw/",
    "data/derived/",
    "output/",
)
SECRET_PATTERNS = {
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "openai-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "authorization": re.compile(r"Authorization:\s*(?:Bearer|Basic)\s+[^\s]{12,}", re.IGNORECASE),
    "signed-query": re.compile(
        r"[?&](?:token|access_token|sig|signature|x-amz-signature)=[^&\s]{16,}",
        re.IGNORECASE,
    ),
}
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml", ".json", ".jsonl", ".txt"}


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

    def _parse_front_matter(self, relative: str) -> dict[str, Any] | None:
        path = self.root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.add("DOCUMENT_READ", relative, str(exc))
            return None
        match = FRONT_MATTER_RE.match(text)
        if not match:
            self.add(
                "FRONT_MATTER_MISSING",
                relative,
                "registered Markdown document lacks YAML front matter",
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
                    "REGISTRY_ENTRY_INVALID", registry_path, f"entry {index} must be a mapping"
                )
                continue
            doc_id = entry.get("document_id")
            relative = entry.get("path")
            if not isinstance(doc_id, str) or not isinstance(relative, str):
                self.add(
                    "REGISTRY_ENTRY_INVALID", registry_path, f"entry {index} lacks document_id/path"
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
                        "CANONICAL_FOR_DUPLICATE", relative, f"{cfor}: also {canonical_for[cfor]}"
                    )
                else:
                    canonical_for[cfor] = relative
            if relative not in self.tracked_files or not (self.root / relative).is_file():
                self.add("REGISTERED_PATH_MISSING", relative, f"registered by {doc_id}")
                continue
            if relative.endswith(".md"):
                front = self._parse_front_matter(relative)
                if front is None:
                    continue
                missing = REQUIRED_DOCUMENT_FIELDS - set(front)
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
                if created is not None and updated is not None and created > updated:
                    self.add("DOCUMENT_TIME_ORDER", relative, "created_at is after updated_at")

        valid_ids = set(entries)
        for doc_id, entry in entries.items():
            relative = entry["path"]
            if not relative.endswith(".md") or not (self.root / relative).is_file():
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
        required_cases = policy.get("required_eval_cases", 2)
        skills = data.get("skills")
        if not isinstance(skills, list):
            self.add("SKILL_SYSTEM_INVALID", relative, "skills must be an array of tables")
            return
        ids: set[str] = set()
        paths: set[str] = set()
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
