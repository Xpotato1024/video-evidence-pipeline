"""Stricter runner-generation views; canonical import contracts remain unchanged."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from video_evidence_pipeline.schemas.generation import SCHEMA_MODELS, schema_bytes
from video_evidence_pipeline.storage.atomic import atomic_write_bytes


def structured_output_schema(schema: dict) -> dict:
    """Require all object properties without altering constraints or instance data."""
    result = deepcopy(schema)

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return
        # Only schema-bearing keywords: defaults/examples/const/enum are instance data.
        for keyword in (
            "$defs",
            "definitions",
            "properties",
            "patternProperties",
            "dependentSchemas",
        ):
            for child in node.get(keyword, {}).values():
                visit(child)
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for child in node.get(keyword, []):
                visit(child)
        for keyword in (
            "items",
            "contains",
            "additionalProperties",
            "additionalItems",
            "unevaluatedProperties",
            "unevaluatedItems",
            "propertyNames",
            "not",
            "if",
            "then",
            "else",
        ):
            child = node.get(keyword)
            if isinstance(child, list):
                for item in child:
                    visit(item)
            else:
                visit(child)
        schema_type = node.get("type")
        if (
            schema_type == "object"
            or (isinstance(schema_type, list) and "object" in schema_type)
            or "properties" in node
        ):
            node["additionalProperties"] = False
            node["required"] = sorted(node.get("properties", {}))

    visit(result)
    return result


def structured_output_schema_bytes(filename: str) -> bytes:
    canonical = json.loads(schema_bytes(filename, SCHEMA_MODELS[filename]))
    return (
        json.dumps(
            structured_output_schema(canonical),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def export_structured_output_schema(filename: str, target: Path) -> Path:
    """Atomically export deterministic bytes to a private runner schema path."""
    atomic_write_bytes(target, structured_output_schema_bytes(filename), private=True)
    return target
