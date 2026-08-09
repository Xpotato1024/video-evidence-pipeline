"""Fail-closed source adapter registry."""

from video_evidence_pipeline.acquisition.base import AcquisitionAdapter
from video_evidence_pipeline.acquisition.box import BoxAdapter
from video_evidence_pipeline.acquisition.direct_http import DirectHttpAdapter
from video_evidence_pipeline.acquisition.generic_ytdlp import GenericYtdlpAdapter
from video_evidence_pipeline.acquisition.local_file import LocalFileAdapter
from video_evidence_pipeline.acquisition.panopto import PanoptoAdapter
from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.domain.job import SourceKind, SourceSpec
from video_evidence_pipeline.errors import configuration_error


def adapter_for(
    source: SourceSpec,
    runtime: AcquisitionRuntimeConfig | None = None,
) -> AcquisitionAdapter:
    if source.kind is SourceKind.LOCAL_FILE:
        return LocalFileAdapter()
    effective_runtime = runtime or AcquisitionRuntimeConfig()
    if source.kind is SourceKind.DIRECT_HTTP:
        return DirectHttpAdapter(effective_runtime)
    if source.kind is SourceKind.GENERIC_YTDLP:
        return GenericYtdlpAdapter(effective_runtime)
    if source.kind is SourceKind.PANOPTO:
        return PanoptoAdapter(effective_runtime)
    if source.kind is SourceKind.BOX:
        return BoxAdapter(effective_runtime)
    raise configuration_error("SOURCE_KIND_NOT_IMPLEMENTED", "source kind is not implemented")
