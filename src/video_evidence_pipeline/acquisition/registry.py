"""Fail-closed source adapter registry."""

from video_evidence_pipeline.acquisition.base import AcquisitionAdapter
from video_evidence_pipeline.acquisition.local_file import LocalFileAdapter
from video_evidence_pipeline.domain.job import SourceKind, SourceSpec
from video_evidence_pipeline.errors import configuration_error


def adapter_for(source: SourceSpec) -> AcquisitionAdapter:
    if source.kind is SourceKind.LOCAL_FILE:
        return LocalFileAdapter()
    raise configuration_error(
        "SOURCE_KIND_NOT_IMPLEMENTED",
        f"source kind '{source.kind.value}' is not implemented",
    )
