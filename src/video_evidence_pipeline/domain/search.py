"""Strict contracts for the private rebuildable SQLite search cache."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, field_validator, model_validator

from video_evidence_pipeline.domain import StrictModel, UtcDateTime
from video_evidence_pipeline.domain.artifacts import ArtifactRef, EvidenceLedgerArtifactRef
from video_evidence_pipeline.domain.identifiers import JobId, Sha256


class SearchDocumentKind(StrEnum):
    TRANSCRIPT = "transcript"
    EVIDENCE = "evidence"


class SearchQueryMode(StrEnum):
    FTS5_LITERAL_MATCH = "fts5_literal_match"
    BOUNDED_LITERAL_SCAN = "bounded_literal_scan"


class SearchIndexMetadata(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: JobId
    transcript_segments_artifact: ArtifactRef
    evidence_artifact: EvidenceLedgerArtifactRef
    transcription_fingerprint: Sha256
    transcript_bundle_fingerprint: Sha256
    evidence_bundle_fingerprint: Sha256
    semantic_fingerprint: Sha256
    documents_fingerprint: Sha256
    query_version: Literal["literal-trigram-query-v1"] = "literal-trigram-query-v1"
    builder_version: Literal["external-content-fts5-builder-v1"] = (
        "external-content-fts5-builder-v1"
    )
    implementation_version: str = Field(min_length=1, max_length=128)
    tokenizer: Literal["trigram"] = "trigram"
    normalizer: Literal["unicode-nfkc-whitespace-v1"] = "unicode-nfkc-whitespace-v1"
    sqlite_version: str = Field(min_length=1, max_length=64)
    sqlite_capability: Literal["fts5-trigram"] = "fts5-trigram"
    fts5_available: Literal[True] = True
    trigram_available: Literal[True] = True
    short_query_scan_limit_per_kind: Literal[4096] = 4096
    indexed_utf8_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    maximum_indexed_utf8_bytes: Literal[67108864] = 64 * 1024 * 1024
    transcript_count: int = Field(ge=1, le=100_000)
    evidence_count: int = Field(ge=0, le=100_000)
    complete: Literal[True] = True
    built_at: UtcDateTime


class SearchIndexBuildResult(StrictModel):
    metadata: SearchIndexMetadata
    reused: StrictBool


class SearchHit(StrictModel):
    rank: int = Field(ge=1, le=100)
    kind: SearchDocumentKind
    record_id: str = Field(min_length=1, max_length=7)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    content: str = Field(min_length=1, max_length=65536)
    score: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def shape_matches_kind(self) -> Self:
        prefix = "T" if self.kind is SearchDocumentKind.TRANSCRIPT else "E"
        if (
            len(self.record_id) != 7
            or not self.record_id.startswith(prefix)
            or not self.record_id[1:].isdigit()
        ):
            raise ValueError("search result record ID does not match its kind")
        if self.kind is SearchDocumentKind.TRANSCRIPT:
            if (self.start_ms is None) != (self.end_ms is None) or (
                self.start_ms is not None
                and self.end_ms is not None
                and self.end_ms <= self.start_ms
            ):
                raise ValueError("transcript search result interval is invalid")
        elif self.start_ms is not None or self.end_ms is not None:
            raise ValueError("evidence search result must not infer an interval")
        return self


class SearchResult(StrictModel):
    metadata: SearchIndexMetadata
    query: str = Field(min_length=1, max_length=256)
    normalized_query: str = Field(min_length=1, max_length=256)
    mode: SearchQueryMode
    limit: int = Field(ge=1, le=100)
    scan_limit_per_kind: int | None = Field(default=None, ge=1)
    truncated: StrictBool
    hits: list[SearchHit] = Field(default_factory=list, max_length=100)

    @field_validator("query", "normalized_query")
    @classmethod
    def query_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("search query must not be blank")
        return value

    @model_validator(mode="after")
    def result_shape_is_consistent(self) -> Self:
        expected_ranks = list(range(1, len(self.hits) + 1))
        if [item.rank for item in self.hits] != expected_ranks or len(self.hits) > self.limit:
            raise ValueError("search result ranks or limit are inconsistent")
        if self.mode is SearchQueryMode.BOUNDED_LITERAL_SCAN:
            if self.scan_limit_per_kind is None:
                raise ValueError("bounded search result requires its scan limit")
        elif self.scan_limit_per_kind is not None:
            raise ValueError("FTS search result must not claim a scan limit")
        return self
