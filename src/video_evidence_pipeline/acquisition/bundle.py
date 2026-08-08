"""Source-kind-aware acquired bundle verification and reuse."""

from video_evidence_pipeline.acquisition.base import AcquisitionResult
from video_evidence_pipeline.acquisition.local_file import verify_acquired_bundle as verify_local
from video_evidence_pipeline.acquisition.staged import recover_network_result, verify_network_bundle
from video_evidence_pipeline.domain.artifacts import ArtifactRef
from video_evidence_pipeline.domain.job import JobSpec, SourceKind
from video_evidence_pipeline.storage.layout import JobWorkspace


def verify_acquired_bundle(workspace: JobWorkspace, job: JobSpec) -> ArtifactRef:
    if job.source.kind is SourceKind.LOCAL_FILE:
        return verify_local(workspace, job)
    return verify_network_bundle(workspace, job)


def reuse_acquired_network_bundle(
    workspace: JobWorkspace,
    job: JobSpec,
) -> AcquisitionResult | None:
    if job.source.kind is SourceKind.LOCAL_FILE:
        return None
    return recover_network_result(workspace, job)
