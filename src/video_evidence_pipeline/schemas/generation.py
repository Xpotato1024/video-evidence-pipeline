"""Deterministic JSON Schema generation from Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from video_evidence_pipeline.domain.artifacts import ArtifactRecord
from video_evidence_pipeline.domain.authorization import AuthorizationRecord
from video_evidence_pipeline.domain.job import JobSpec
from video_evidence_pipeline.domain.manifests import SourceManifest, StageManifest
from video_evidence_pipeline.domain.media import (
    FrameIndex,
    FrameRecord,
    MediaManifest,
    MediaProbe,
    MediaStream,
    NormalizationConfig,
    NormalizedSubtitle,
)
from video_evidence_pipeline.domain.transitions import TransitionRecord
from video_evidence_pipeline.errors import configuration_error
from video_evidence_pipeline.storage.atomic import atomic_write_bytes

SCHEMA_VERSION = "1.0"
SCHEMA_BASE_ID = "https://schemas.video-evidence-pipeline.local/v1"

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "job-spec.schema.json": JobSpec,
    "authorization-record.schema.json": AuthorizationRecord,
    "artifact-record.schema.json": ArtifactRecord,
    "source-manifest.schema.json": SourceManifest,
    "stage-manifest.schema.json": StageManifest,
    "transition-record.schema.json": TransitionRecord,
    "media-probe.schema.json": MediaProbe,
    "media-stream.schema.json": MediaStream,
    "media-manifest.schema.json": MediaManifest,
    "normalized-subtitle.schema.json": NormalizedSubtitle,
    "frame-record.schema.json": FrameRecord,
    "frame-index.schema.json": FrameIndex,
    "normalization-config.schema.json": NormalizationConfig,
}


def schema_bytes(filename: str, model: type[BaseModel]) -> bytes:
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"{SCHEMA_BASE_ID}/{filename}"
    schema["x-schema-version"] = SCHEMA_VERSION
    return (
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def generated_schemas() -> dict[str, bytes]:
    return {name: schema_bytes(name, model) for name, model in sorted(SCHEMA_MODELS.items())}


def export_schemas(root: Path) -> list[str]:
    written: list[str] = []
    for name, payload in generated_schemas().items():
        atomic_write_bytes(root / name, payload)
        written.append(name)
    return written


def check_schemas(root: Path) -> list[str]:
    drift: list[str] = []
    expected_names = set(SCHEMA_MODELS)
    for name, expected in generated_schemas().items():
        path = root / name
        try:
            actual = path.read_bytes()
        except FileNotFoundError:
            drift.append(name)
            continue
        if actual != expected:
            drift.append(name)
    drift.extend(
        path.name for path in root.glob("*.schema.json") if path.name not in expected_names
    )
    if drift:
        raise configuration_error("SCHEMA_DRIFT", "generated JSON Schema is out of date")
    return sorted(SCHEMA_MODELS)
