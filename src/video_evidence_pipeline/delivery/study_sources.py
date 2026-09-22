"""Resolve semantic source IDs to portable archive payloads."""

from __future__ import annotations

import json
from pathlib import Path

from video_evidence_pipeline.domain.evidence import SourceRef, SourceRefKind
from video_evidence_pipeline.domain.serialization import fingerprint
from video_evidence_pipeline.domain.study_bundle import StudySourceBinding, StudySourceIndex
from video_evidence_pipeline.domain.transcription import TranscriptSegment
from video_evidence_pipeline.errors import integrity_error


def _source_refs(value):
    if isinstance(value, dict):
        if {"kind", "ref_id", "content_sha256"} <= value.keys():
            yield SourceRef.model_validate(value)
        else:
            for child in value.values():
                yield from _source_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _source_refs(child)


def build_source_index(root: Path, inventory) -> StudySourceIndex:
    from video_evidence_pipeline.delivery.study_bundle import STRUCTURED_PREFIXES, _read

    segments = {}
    assets = {}
    references = {}
    for relative, entry in sorted(inventory.items()):
        if relative.startswith(("supporting/", "normalized/frames/")):
            assets.setdefault(entry.sha256, relative)
        if not relative.startswith(STRUCTURED_PREFIXES) or Path(relative).suffix not in {
            ".json",
            ".jsonl",
        }:
            continue
        payload = _read(root / relative).decode("utf-8")
        values = (
            [json.loads(line) for line in payload.splitlines()]
            if relative.endswith(".jsonl")
            else [json.loads(payload)]
        )
        if relative.startswith(("transcript/segments/", "refinement/segments/")):
            for value in values:
                segment = TranscriptSegment.model_validate(value)
                ref = SourceRef(
                    kind=SourceRefKind.TRANSCRIPT_SEGMENT,
                    ref_id=segment.segment_id,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    content_sha256=fingerprint(segment),
                )
                key = fingerprint(ref)
                segments[key] = relative
                references[key] = ref
        for value in values:
            for ref in _source_refs(value):
                references[fingerprint(ref)] = ref
    bindings = []
    for key, ref in sorted(references.items()):
        relative = (
            segments.get(key)
            if ref.kind is SourceRefKind.TRANSCRIPT_SEGMENT
            else assets.get(ref.content_sha256)
        )
        if relative is None:
            raise integrity_error(
                "STUDY_BUNDLE_SOURCE_REF_MISSING",
                "semantic source reference cannot be resolved inside archive",
            )
        bindings.append(StudySourceBinding(source_ref=ref, relative_path=relative))
    return StudySourceIndex(bindings=bindings)
