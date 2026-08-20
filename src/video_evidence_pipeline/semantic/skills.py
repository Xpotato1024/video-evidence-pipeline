"""Fail-closed loader for frozen repository-owned Skill contracts."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.domain.semantic import SkillResourceSnapshot, SkillSnapshot
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.errors import integrity_error, state_error

MAXIMUM_SKILL_FILE_BYTES = 1024 * 1024
MAXIMUM_RESOURCE_FILE_BYTES = 4 * 1024 * 1024
MAXIMUM_RESOURCE_BUNDLE_BYTES = 16 * 1024 * 1024
MAXIMUM_RESOURCE_FILES = 128
_RESOURCE_DIRECTORIES = {
    "references": "reference",
    "scripts": "script",
    "assets": "asset",
}
_TEMP_OR_GENERATED = re.compile(
    r"(?:^|[-_.])(?:tmp|temp|generated|cache|backup|bak)(?:$|[-_.])", re.IGNORECASE
)


class WorkspaceWithJobsRoot(Protocol):
    jobs_root: Path


def _repository_root(workspace: WorkspaceWithJobsRoot) -> Path:
    return workspace.jobs_root.parent.parent


def _read_bounded(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    try:
        size = path.stat(follow_symlinks=False).st_size
        if size <= 0 or size > maximum_bytes:
            raise OSError
        return path.read_bytes()
    except OSError:
        raise integrity_error(code, "Skill file is unavailable or exceeds its byte limit") from None


def _canonical_text_sha256(payload: bytes) -> str:
    return sha256_bytes(payload.replace(b"\r\n", b"\n"))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return (
        stat.S_ISLNK(info.st_mode)
        or path.is_symlink()
        or getattr(path, "is_junction", lambda: False)()
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    )


def _validate_contained_regular_file(path: Path, *, root: Path, code: str) -> None:
    try:
        canonical_root = root.resolve(strict=True)
        canonical = path.resolve(strict=True)
        canonical.relative_to(canonical_root)
    except (OSError, ValueError):
        raise integrity_error(code, "Skill path escapes its allowed root") from None
    current = path
    while True:
        if _is_link_or_reparse(current):
            raise integrity_error(code, "Skill paths must not contain a link or junction")
        if current == root:
            break
        if current.parent == current:
            raise integrity_error(code, "Skill path escapes its allowed root")
        current = current.parent
    try:
        if not path.is_file():
            raise OSError
    except OSError:
        raise integrity_error(code, "Skill path is not a regular file") from None


def _resource_declaration(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "type"}:
        raise ValueError
    relative = value["path"]
    resource_type = value["type"]
    if not isinstance(relative, str) or not isinstance(resource_type, str):
        raise ValueError
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or str(pure) != relative
        or any(part in {"", ".", ".."} or part.startswith(".") for part in pure.parts)
        or "\\" in relative
    ):
        raise ValueError
    if resource_type not in set(_RESOURCE_DIRECTORIES.values()):
        raise ValueError
    if _RESOURCE_DIRECTORIES.get(pure.parts[0]) != resource_type:
        raise ValueError
    if any(_TEMP_OR_GENERATED.search(part) for part in pure.parts):
        raise ValueError
    return relative, resource_type


def _declared_resource_snapshots(
    *, skill_path: Path, declarations: Any
) -> list[SkillResourceSnapshot]:
    if declarations is None:
        declarations = []
    if not isinstance(declarations, list) or len(declarations) > MAXIMUM_RESOURCE_FILES:
        raise integrity_error("SKILL_REGISTRY_INVALID", "Skill resource manifest is invalid")
    try:
        parsed = [_resource_declaration(item) for item in declarations]
    except (TypeError, ValueError):
        raise integrity_error(
            "SKILL_REGISTRY_INVALID", "Skill resource manifest is invalid"
        ) from None
    if [path for path, _ in parsed] != sorted({path for path, _ in parsed}):
        raise integrity_error(
            "SKILL_REGISTRY_INVALID", "Skill resource paths must be sorted and unique"
        )
    skill_root = skill_path.parent
    discovered: set[str] = set()
    for directory_name in _RESOURCE_DIRECTORIES:
        directory = skill_root / directory_name
        if not directory.exists():
            continue
        if _is_link_or_reparse(directory) or not directory.is_dir():
            raise integrity_error("SKILL_SNAPSHOT_INVALID", "Skill resource directory is invalid")
        for current, directories, filenames in os.walk(directory, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *filenames]:
                candidate = current_path / name
                if name.startswith(".") or _TEMP_OR_GENERATED.search(name):
                    raise integrity_error(
                        "SKILL_SNAPSHOT_INVALID",
                        "hidden or temporary Skill resources are forbidden",
                    )
                if _is_link_or_reparse(candidate):
                    raise integrity_error(
                        "SKILL_SNAPSHOT_INVALID",
                        "Skill resources must not contain links or junctions",
                    )
            for filename in filenames:
                discovered.add((current_path / filename).relative_to(skill_root).as_posix())
    declared_paths = {path for path, _ in parsed}
    if discovered != declared_paths:
        raise integrity_error(
            "SKILL_SNAPSHOT_INVALID",
            "every bundled Skill resource must be explicitly declared exactly once",
        )
    total_bytes = 0
    snapshots: list[SkillResourceSnapshot] = []
    for relative, resource_type in parsed:
        path = skill_root.joinpath(*relative.split("/"))
        _validate_contained_regular_file(path, root=skill_root, code="SKILL_SNAPSHOT_INVALID")
        payload = _read_bounded(
            path, maximum_bytes=MAXIMUM_RESOURCE_FILE_BYTES, code="SKILL_SNAPSHOT_INVALID"
        )
        total_bytes += len(payload)
        if total_bytes > MAXIMUM_RESOURCE_BUNDLE_BYTES:
            raise integrity_error(
                "SKILL_SNAPSHOT_INVALID", "Skill resource bundle exceeds its byte limit"
            )
        snapshots.append(
            SkillResourceSnapshot(
                relative_path=relative,
                sha256=sha256_bytes(payload),
                type=resource_type,
            )
        )
    return snapshots


def _active_promotion_binding(
    *,
    root: Path,
    entry: dict[str, Any],
    skill_id: str,
    skill_bytes: bytes,
    eval_bytes: bytes,
    resource_bundle_fingerprint: str,
) -> tuple[str, str]:
    result_relative = entry.get("eval_result_path")
    if not isinstance(result_relative, str) or not result_relative:
        raise integrity_error("SKILL_REGISTRY_INVALID", "active Skill lacks evaluation evidence")
    result_path = root.joinpath(*result_relative.replace("\\", "/").split("/"))
    _validate_contained_regular_file(result_path, root=root, code="SKILL_REGISTRY_INVALID")
    result_bytes = _read_bounded(
        result_path, maximum_bytes=MAXIMUM_SKILL_FILE_BYTES, code="SKILL_SNAPSHOT_INVALID"
    )
    try:
        result = tomllib.loads(result_bytes.decode("utf-8"))
        if (
            result["skill_id"] != skill_id
            or result["skill_path"] != entry["path"].replace("\\", "/")
            or result["eval_definition_path"] != entry["eval_path"].replace("\\", "/")
            or result["skill_content_sha256"] != _canonical_text_sha256(skill_bytes)
            or result["eval_definition_sha256"] != _canonical_text_sha256(eval_bytes)
            or result["resource_bundle_fingerprint"] != resource_bundle_fingerprint
            or result["side_effect_policy"] != "instruction-only"
            or result["promotion_eligible"] is not True
            or result["p0_count"] != 0
            or result["p1_count"] != 0
            or not re.fullmatch(r"[0-9a-f]{40}", result["evaluated_commit_sha"])
        ):
            raise ValueError
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
        raise integrity_error(
            "SKILL_EVAL_RESULT_INVALID", "active Skill evaluation evidence is invalid or stale"
        ) from None
    return sha256_bytes(result_bytes), result["evaluated_commit_sha"]


def load_skill_snapshot(
    workspace: WorkspaceWithJobsRoot,
    skill_id: str,
    *,
    allowed: frozenset[str],
) -> SkillSnapshot:
    """Load an allowed Skill and bind all production-semantic bytes."""

    if skill_id not in allowed:
        raise state_error("SKILL_ROUTE_INVALID", "selected Skill is not valid for this stage")
    root = _repository_root(workspace)
    registry_path = root / ".agents" / "skill-system.toml"
    _validate_contained_regular_file(registry_path, root=root, code="SKILL_REGISTRY_INVALID")
    registry_bytes = _read_bounded(
        registry_path, maximum_bytes=MAXIMUM_SKILL_FILE_BYTES, code="SKILL_REGISTRY_INVALID"
    )
    try:
        registry = tomllib.loads(registry_bytes.decode("utf-8"))
        entries = [item for item in registry["skills"] if item.get("id") == skill_id]
        if len(entries) != 1:
            raise ValueError
        entry = entries[0]
        relative_path = entry["path"].replace("\\", "/")
        eval_relative = entry["eval_path"].replace("\\", "/")
        if (
            entry["automatic_trigger"] is not False
            or entry["side_effect_policy"] != "instruction-only"
            or entry["lifecycle"] not in {"draft", "active"}
            or not isinstance(entry["owner"], str)
            or not entry["owner"].strip()
        ):
            raise ValueError
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
        raise integrity_error("SKILL_REGISTRY_INVALID", "Skill registry is invalid") from None
    skill_path = root.joinpath(*relative_path.split("/"))
    eval_path = root.joinpath(*eval_relative.split("/"))
    _validate_contained_regular_file(skill_path, root=root, code="SKILL_REGISTRY_INVALID")
    _validate_contained_regular_file(eval_path, root=root, code="SKILL_REGISTRY_INVALID")
    skill_bytes = _read_bounded(
        skill_path, maximum_bytes=MAXIMUM_SKILL_FILE_BYTES, code="SKILL_SNAPSHOT_INVALID"
    )
    eval_bytes = _read_bounded(
        eval_path, maximum_bytes=MAXIMUM_SKILL_FILE_BYTES, code="SKILL_SNAPSHOT_INVALID"
    )
    resources = _declared_resource_snapshots(
        skill_path=skill_path,
        declarations=entry.get("resources"),
    )
    resource_fingerprint = fingerprint([item.model_dump(mode="json") for item in resources])
    eval_result_sha256: str | None = None
    evaluated_commit_sha: str | None = None
    if entry["lifecycle"] == "active":
        eval_result_sha256, evaluated_commit_sha = _active_promotion_binding(
            root=root,
            entry=entry,
            skill_id=skill_id,
            skill_bytes=skill_bytes,
            eval_bytes=eval_bytes,
            resource_bundle_fingerprint=resource_fingerprint,
        )
    return SkillSnapshot(
        skill_id=skill_id,
        relative_path=relative_path,
        content_sha256=sha256_bytes(skill_bytes),
        eval_sha256=sha256_bytes(eval_bytes),
        lifecycle=entry["lifecycle"],
        automatic_trigger=False,
        side_effect_policy="instruction-only",
        explicit_selection=True,
        owner=entry["owner"],
        resources=resources,
        resource_bundle_fingerprint=resource_fingerprint,
        eval_result_sha256=eval_result_sha256,
        evaluated_commit_sha=evaluated_commit_sha,
    )


def same_skill_snapshot_except_lifecycle(frozen: SkillSnapshot, current: SkillSnapshot) -> bool:
    """Compare immutable execution provenance while allowing promotion-only drift."""

    promotion_fields = {"lifecycle"}
    if frozen.eval_result_sha256 is None and frozen.evaluated_commit_sha is None:
        promotion_fields.update({"eval_result_sha256", "evaluated_commit_sha"})
    return frozen.model_dump(mode="json", exclude=promotion_fields) == current.model_dump(
        mode="json", exclude=promotion_fields
    )


def validate_import_skill(
    workspace: WorkspaceWithJobsRoot,
    skill: SkillSnapshot,
    *,
    allowed: frozenset[str],
) -> None:
    """Require a frozen request Skill to remain the current active Skill."""

    current = load_skill_snapshot(workspace, skill.skill_id, allowed=allowed)
    if not same_skill_snapshot_except_lifecycle(skill, current):
        raise integrity_error(
            "SKILL_SNAPSHOT_STALE",
            "current Skill content, resources, evaluation, or identity differs from the request snapshot",
        )
    if current.lifecycle != "active":
        raise state_error(
            "DRAFT_SKILL_FORBIDDEN",
            "draft Skill output is not allowed on a production import route",
        )
