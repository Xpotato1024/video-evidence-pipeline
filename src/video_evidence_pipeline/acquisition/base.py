"""Backend-neutral acquisition interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from video_evidence_pipeline.domain.job import JobSpec
from video_evidence_pipeline.domain.manifests import SourceManifest, StageManifest
from video_evidence_pipeline.storage.layout import JobWorkspace


@dataclass(frozen=True, slots=True)
class ProbeResult:
    display_name: str
    locator_fingerprint: str
    declared_size_bytes: int | None = None
    content_type: str | None = None
    content_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    source_manifest: SourceManifest
    stage_manifest: StageManifest
    reused: bool


class AcquisitionAdapter(ABC):
    @abstractmethod
    def probe(self, job: JobSpec) -> ProbeResult:
        """Validate and fingerprint a source without changing the workspace."""

    @abstractmethod
    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        """Acquire an immutable source into the workspace."""
