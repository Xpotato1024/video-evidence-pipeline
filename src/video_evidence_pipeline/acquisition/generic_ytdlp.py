"""Generic webpage acquisition through the hardened yt-dlp runner."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

from video_evidence_pipeline.acquisition.base import (
    AcquisitionAdapter,
    AcquisitionResult,
    ProbeResult,
)
from video_evidence_pipeline.acquisition.network_policy import Resolver, default_resolver
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
)
from video_evidence_pipeline.acquisition.staged import publish_staged_source
from video_evidence_pipeline.acquisition.ytdlp_runner import (
    SubprocessRunner,
    YtdlpToolchainFactory,
    default_ytdlp_toolchain,
    run_ytdlp,
)
from video_evidence_pipeline.domain.job import JobSpec, SourceKind
from video_evidence_pipeline.domain.locators import locator_fingerprint
from video_evidence_pipeline.errors import integrity_error
from video_evidence_pipeline.storage.layout import JobWorkspace


class GenericYtdlpAdapter(AcquisitionAdapter):
    def __init__(
        self,
        runtime: AcquisitionRuntimeConfig,
        *,
        runner: SubprocessRunner | None = None,
        resolver: Resolver = default_resolver,
        monotonic: Callable[[], float] = time.monotonic,
        toolchain_factory: YtdlpToolchainFactory = default_ytdlp_toolchain,
    ) -> None:
        self.runtime = runtime
        self.runner = runner
        self.resolver = resolver
        self.monotonic = monotonic
        self.toolchain_factory = toolchain_factory

    def probe(self, job: JobSpec) -> ProbeResult:
        if job.source.kind is not SourceKind.GENERIC_YTDLP:
            raise integrity_error("ADAPTER_KIND_MISMATCH", "yt-dlp adapter kind mismatch")
        host = urlsplit(job.source.locator).hostname or "source"
        return ProbeResult(
            display_name=f"{host}.media",
            locator_fingerprint=locator_fingerprint(job.source.locator),
        )

    def acquire(self, job: JobSpec, workspace: JobWorkspace) -> AcquisitionResult:
        started_at = datetime.now(UTC)
        deadline = AcquisitionDeadline.start(
            self.runtime.maximum_download_seconds,
            monotonic=self.monotonic,
        )
        self.probe(job)
        staged = run_ytdlp(
            job.source.locator,
            workspace,
            self.runtime,
            adapter_kind=SourceKind.GENERIC_YTDLP,
            runner=self.runner,
            resolver=self.resolver,
            deadline=deadline,
            toolchain_factory=self.toolchain_factory,
        )
        return publish_staged_source(
            job,
            workspace,
            staged,
            maximum_bytes=self.runtime.maximum_download_bytes,
            started_at=started_at,
        )
