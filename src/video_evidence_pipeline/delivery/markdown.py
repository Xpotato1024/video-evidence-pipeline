"""Byte-exact Markdown export adapter boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from video_evidence_pipeline import __version__
from video_evidence_pipeline.domain.artifacts import ArtifactRef, ProducerIdentity, ProducerKind
from video_evidence_pipeline.domain.delivery import ExportAdapterIdentity
from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.errors import integrity_error, io_error
from video_evidence_pipeline.storage.atomic import atomic_copy_exclusive


class DeliveryAdapter(Protocol):
    identity: ExportAdapterIdentity
    producer: ProducerIdentity

    def copy_exact(self, source: Path, target: Path, expected: ArtifactRef) -> None: ...


class MarkdownByteExactAdapter:
    """Copy approved Markdown as opaque bytes; never decode or transform it."""

    identity = ExportAdapterIdentity(version=__version__)
    producer = ProducerIdentity(
        kind=ProducerKind.ADAPTER,
        name=identity.name,
        version=identity.version,
    )

    @staticmethod
    def _verify(path: Path, expected: ArtifactRef) -> None:
        try:
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise integrity_error("EXPORT_BUNDLE_INVALID", "export bytes are absent") from exc
        except OSError as exc:
            raise io_error("EXPORT_BUNDLE_INVALID", "export bytes could not be inspected") from exc
        if size != expected.size_bytes or sha256_file(path) != expected.content_sha256:
            raise integrity_error("EXPORT_BUNDLE_INVALID", "export bytes are not byte-exact")

    def copy_exact(self, source: Path, target: Path, expected: ArtifactRef) -> None:
        self._verify(source, expected)
        if target.exists():
            self._verify(target, expected)
            return
        atomic_copy_exclusive(
            source,
            target,
            verifier=lambda path: self._verify(path, expected),
        )
        self._verify(target, expected)
