"""Explicit installed resources/data; source-checkout defaults remain unchanged.

The native development launcher supplies this context to a private worker. It is
not an authorization token: media/API permissions still belong to each job.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.errors import configuration_error, integrity_error


@dataclass(frozen=True, slots=True)
class ResourceContext:
    root: Path


@dataclass(frozen=True, slots=True)
class DataContext:
    root: Path
    bindings_root: Path

    @property
    def jobs_root(self) -> Path:
        return self.root / "jobs"

    @property
    def models_root(self) -> Path:
        return self.root / "models"


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    release_id: str
    resources: ResourceContext
    data: DataContext
    protected_roots: tuple[Path, ...] = ()

    def binding_path(self, job_root: Path) -> Path:
        identity = os.path.normcase(str(job_root.resolve())).encode("utf-8")
        return self.data.bindings_root / f"{sha256_bytes(identity)}.json"

    def binding(self, job_root: Path) -> dict[str, str]:
        return {
            "schema": "vep.job-runtime.v1",
            "release_id": self.release_id,
            "resource_root": str(self.resources.root.resolve()),
            "job_root": str(job_root.resolve()),
        }

    def require_binding(self, job_root: Path) -> None:
        path = self.binding_path(job_root)
        from video_evidence_pipeline.storage.layout import is_link_or_junction

        try:
            if is_link_or_junction(path) or path.stat().st_size > 16 * 1024:
                raise ValueError
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise configuration_error(
                "JOB_RUNTIME_BINDING_REQUIRED",
                "use the original source checkout; this job has no verified installed runtime binding",
            ) from None
        if observed != self.binding(job_root):
            raise integrity_error(
                "JOB_RUNTIME_MISMATCH", "open this job using its pinned runtime and resources"
            )


_CURRENT: ContextVar[RuntimeContext | None] = ContextVar("vep_runtime", default=None)


def current_runtime() -> RuntimeContext | None:
    return _CURRENT.get()


@contextmanager
def use_runtime(context: RuntimeContext) -> Iterator[None]:
    """Scope explicit roots to one worker invocation, including embedded CLI tests."""
    from video_evidence_pipeline.storage.layout import _assert_no_links

    roots = (context.resources.root, context.data.root, context.data.bindings_root)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", context.release_id):
        raise configuration_error("RUNTIME_CONTEXT_INVALID", "runtime context is invalid")
    for root in roots:
        if not root.is_absolute() or not root.is_dir():
            raise configuration_error("RUNTIME_CONTEXT_INVALID", "runtime roots must already exist")
        _assert_no_links(Path(root.anchor), root)
    normalized = tuple(root.resolve() for root in roots)
    if any(
        a.is_relative_to(b) or b.is_relative_to(a)
        for i, a in enumerate(normalized)
        for b in normalized[i + 1 :]
    ):
        raise configuration_error("RUNTIME_CONTEXT_INVALID", "runtime roots must be disjoint")
    for protected in context.protected_roots:
        _assert_no_links(Path(protected.anchor), protected)
        if context.data.root.resolve().is_relative_to(
            protected.resolve()
        ) or protected.resolve().is_relative_to(context.data.root.resolve()):
            raise configuration_error(
                "RUNTIME_CONTEXT_INVALID", "data must remain outside app/state"
            )
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def resource_root(fallback: Path) -> Path:
    context = current_runtime()
    return context.resources.root if context else fallback


def model_root(fallback_repository: Path) -> Path:
    context = current_runtime()
    return context.data.models_root if context else fallback_repository / ".local" / "models"


def require_source_asr() -> None:
    """Keep unvalidated installed ASR/build-context paths behind their phase gate."""
    if current_runtime() is not None:
        raise configuration_error(
            "ASR_INSTALLED_COMPONENT_NOT_READY",
            "installed ASR provisioning/build is not implemented; use the source checkout",
        )
