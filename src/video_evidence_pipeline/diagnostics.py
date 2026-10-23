"""Read-only installation diagnostics; never acquire data or provision tools."""

from __future__ import annotations

import platform
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from video_evidence_pipeline import __version__
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.errors import VepError, state_error
from video_evidence_pipeline.runtime_context import current_runtime, resource_root
from video_evidence_pipeline.semantic.skills import load_skill_snapshot

_SEMANTIC_SKILLS = (
    "refine-video-transcript",
    "analyze-video-evidence",
    "draft-video-summary",
    "draft-video-report",
    "independent-content-audit",
    "revise-from-audit",
)


def diagnose_installation(repository_root: Path, *, media: bool = False) -> dict[str, Any]:
    """Report prerequisites for this checkout, without touching any job bytes.

    A successful report covers installation only. It does not authorize a source,
    validate a job, or assert that an ASR image/model/device is ready.
    """
    root = resource_root(repository_root).resolve()
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, code: str, remedy: str, **details: Any) -> None:
        checks.append(
            {
                "name": name,
                "ok": ok,
                "code": "OK" if ok else code,
                "remedy": "" if ok else remedy,
                **details,
            }
        )

    record(
        "python",
        (3, 12) <= sys.version_info[:2] < (3, 14),
        "PYTHON_UNSUPPORTED",
        "Use Python 3.12 or 3.13; run uv sync --locked --no-dev --python 3.12.",
        version=platform.python_version(),
    )
    required = (
        "pyproject.toml",
        "uv.lock",
        ".agents/skill-system.toml",
        "config/asr-profiles.yaml",
    )
    missing = [name for name in required if not (root / name).is_file()]
    record(
        "repository",
        not missing,
        "REPOSITORY_INCOMPLETE",
        "Use the complete source checkout, including .agents, config, schemas and uv.lock.",
        missing=missing,
    )
    try:
        source = Path(__file__).resolve()
        source.relative_to(root / "src")
        manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        matching = manifest["project"]["version"] == __version__
    except (OSError, ValueError, KeyError, TypeError):
        matching = False
    record(
        "checkout_runtime",
        matching,
        "CHECKOUT_RUNTIME_MISMATCH",
        "Run uv sync --locked --no-dev in this checkout, then uv run --no-sync vep doctor.",
    )
    for skill in _SEMANTIC_SKILLS:
        try:
            # Loader only reads: no JobWorkspace is created by this diagnostic.
            snapshot = load_skill_snapshot(
                SimpleNamespace(jobs_root=root / ".local/jobs"), skill, allowed=frozenset({skill})
            )
            if snapshot.lifecycle != "active":
                raise state_error(
                    "DRAFT_SKILL_FORBIDDEN", "semantic Skill must be active for production use"
                )
        except VepError as exc:
            record(
                skill,
                False,
                exc.code,
                "Restore this revision's complete .agents directory; do not patch old requests.",
            )
        else:
            record(skill, True, "", "")
    try:
        from video_evidence_pipeline.schemas.generation import check_schemas

        check_schemas(root / "schemas")
    except (VepError, OSError):
        record(
            "schemas",
            False,
            "SCHEMA_DRIFT",
            "Restore schemas from the same source revision as the CLI.",
        )
    else:
        record("schemas", True, "", "")
    if media:
        try:
            from video_evidence_pipeline.media.runner import FFmpegRunner

            tools = FFmpegRunner().discover_identities(
                timeout_seconds=30, output_limit_bytes=4 * 1024 * 1024
            )
        except VepError as exc:
            record(
                "ffmpeg",
                False,
                exc.code,
                "Install a supported ffmpeg/ffprobe pair; see docs/operations/installation-and-updates.md.",
            )
        else:
            record("ffmpeg", True, "", "", tools=[tool.model_dump(mode="json") for tool in tools])
    lock = root / "uv.lock"
    try:
        lock_sha256 = sha256_file(lock) if lock.is_file() else None
    except OSError:
        lock_sha256 = None
        record(
            "lock_readable",
            False,
            "LOCK_UNREADABLE",
            "Restore a readable uv.lock from this revision.",
        )
    return {
        "ok": all(check["ok"] for check in checks),
        "vep_version": __version__,
        "installation_kind": "private-runtime" if current_runtime() else "source-checkout",
        "platform": sys.platform,
        "lock_sha256": lock_sha256,
        "mode": "media" if media else "transcript",
        "checks": checks,
        "asr": "not checked; when needed, use vep asr image-smoke and gpu-preflight explicitly",
    }
