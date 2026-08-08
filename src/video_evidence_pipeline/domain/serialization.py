"""Deterministic JSON serialization and semantic fingerprints."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from video_evidence_pipeline.domain.hashing import sha256_bytes


def normalized_value(value: BaseModel | Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    return value


def canonical_json_bytes(value: BaseModel | Any) -> bytes:
    return json.dumps(
        normalized_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def pretty_json_bytes(value: BaseModel | Any) -> bytes:
    return (
        json.dumps(
            normalized_value(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def fingerprint(value: BaseModel | Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))
