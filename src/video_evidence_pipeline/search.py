"""Private, rebuildable SQLite FTS5 search over transcript and evidence text."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from video_evidence_pipeline import __version__
from video_evidence_pipeline.domain.evidence import EvidenceManifest, EvidenceRecord
from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.domain.search import (
    SearchDocumentKind,
    SearchHit,
    SearchIndexBuildResult,
    SearchIndexMetadata,
    SearchQueryMode,
    SearchResult,
)
from video_evidence_pipeline.domain.serialization import canonical_json_bytes, fingerprint
from video_evidence_pipeline.domain.transcription import TranscriptManifest, TranscriptSegment
from video_evidence_pipeline.errors import VepError, integrity_error, io_error
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.locking import exclusive_file_lock
from video_evidence_pipeline.storage.permissions import (
    PRIVATE_FILE_MODE,
    enforce_private_descriptor,
    enforce_private_file,
    ensure_private_directory,
)

SEARCH_QUERY_VERSION = "literal-trigram-query-v1"
SEARCH_BUILDER_VERSION = "external-content-fts5-builder-v1"
SEARCH_IMPLEMENTATION_VERSION = f"{__version__}:search-v1"
SEARCH_TOKENIZER = "trigram"
SEARCH_NORMALIZER = "unicode-nfkc-whitespace-v1"
SQLITE_CAPABILITY = "fts5-trigram"

MAX_QUERY_CHARS = 256
MAX_QUERY_UTF8_BYTES = 1024
MAX_SEARCH_LIMIT = 100
SHORT_QUERY_SCAN_LIMIT_PER_KIND = 4096
MAX_INDEXED_UTF8_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 512 * 1024 * 1024

_EVIDENCE_ROWID_OFFSET = 1_000_000_000
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_STAGING_FILE_NAME = re.compile(r"^[0-9a-f]{32}\.sqlite3(?:-journal|-wal|-shm)?$")

_CREATE_DOCUMENTS_SQL = """
CREATE TABLE documents (
    rowid INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('transcript', 'evidence')),
    record_id TEXT NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 65536),
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE (kind, record_id),
    CHECK (
        (
            kind = 'transcript'
            AND record_id GLOB 'T[0-9][0-9][0-9][0-9][0-9][0-9]'
            AND (
                (start_ms IS NULL AND end_ms IS NULL)
                OR (
                    start_ms IS NOT NULL
                    AND end_ms IS NOT NULL
                    AND start_ms >= 0
                    AND end_ms > start_ms
                )
            )
        )
        OR
        (
            kind = 'evidence'
            AND record_id GLOB 'E[0-9][0-9][0-9][0-9][0-9][0-9]'
            AND start_ms IS NULL
            AND end_ms IS NULL
        )
    )
) STRICT
"""

_CREATE_METADATA_SQL = """
CREATE TABLE index_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    metadata_json TEXT NOT NULL CHECK (length(metadata_json) BETWEEN 2 AND 65536),
    semantic_fingerprint TEXT NOT NULL CHECK (
        length(semantic_fingerprint) = 64
        AND semantic_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    complete INTEGER NOT NULL CHECK (complete = 1)
) STRICT
"""

_CREATE_FTS_SQL = """
CREATE VIRTUAL TABLE documents_fts USING fts5(
    content,
    content = 'documents',
    content_rowid = 'rowid',
    tokenize = 'trigram'
)
"""

_INSERT_DOCUMENT_SQL = """
INSERT INTO documents(rowid, kind, record_id, start_ms, end_ms, content, content_sha256)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_FTS_SEARCH_SQL = """
SELECT
    d.rowid,
    d.kind,
    d.record_id,
    d.start_ms,
    d.end_ms,
    d.content,
    d.content_sha256,
    bm25(documents_fts) AS score
FROM documents_fts
JOIN documents AS d ON d.rowid = documents_fts.rowid
WHERE documents_fts MATCH ?
ORDER BY score ASC, d.rowid ASC
LIMIT ?
"""

_SHORT_SEARCH_SQL = """
WITH bounded AS MATERIALIZED (
    SELECT * FROM (
        SELECT rowid, kind, record_id, start_ms, end_ms, content, content_sha256
        FROM documents
        WHERE kind = ?
        ORDER BY rowid ASC
        LIMIT ?
    )
    UNION ALL
    SELECT * FROM (
        SELECT rowid, kind, record_id, start_ms, end_ms, content, content_sha256
        FROM documents
        WHERE kind = ?
        ORDER BY rowid ASC
        LIMIT ?
    )
)
SELECT rowid, kind, record_id, start_ms, end_ms, content, content_sha256, NULL AS score
FROM bounded
WHERE instr(content, ?) > 0
ORDER BY rowid ASC
LIMIT ?
"""


@dataclass(frozen=True, slots=True)
class _Document:
    rowid: int
    kind: SearchDocumentKind
    record_id: str
    start_ms: int | None
    end_ms: int | None
    content: str
    content_sha256: str

    def database_values(self) -> tuple[int, str, str, int | None, int | None, str, str]:
        return (
            self.rowid,
            self.kind.value,
            self.record_id,
            self.start_ms,
            self.end_ms,
            self.content,
            self.content_sha256,
        )

    def fingerprint_value(self) -> dict[str, object]:
        return {
            "rowid": self.rowid,
            "kind": self.kind.value,
            "record_id": self.record_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "content": self.content,
            "content_sha256": self.content_sha256,
        }


def _index_invalid(message: str = "search index is invalid") -> VepError:
    return integrity_error("INDEX_INVALID", message)


def _normalizer(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _canonical_jsonl_identity(
    records: Sequence[TranscriptSegment | EvidenceRecord],
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for record in records:
        line = canonical_json_bytes(record) + b"\n"
        digest.update(line)
        size += len(line)
    return size, digest.hexdigest()


def _verify_canonical_artifact(
    records: Sequence[TranscriptSegment | EvidenceRecord],
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    size, content_sha256 = _canonical_jsonl_identity(records)
    if size != expected_size or content_sha256 != expected_sha256:
        raise _index_invalid("search source artifact does not match its canonical records")


def _rowid(kind: SearchDocumentKind, record_id: str) -> int:
    ordinal = int(record_id[1:])
    if kind is SearchDocumentKind.TRANSCRIPT:
        return ordinal
    return _EVIDENCE_ROWID_OFFSET + ordinal


def _validate_sources(
    job_id: str,
    transcript_manifest: TranscriptManifest,
    segments: Sequence[TranscriptSegment],
    evidence_manifest: EvidenceManifest,
    evidence_records: Sequence[EvidenceRecord],
) -> tuple[_Document, ...]:
    segment_values = tuple(segments)
    evidence_values = tuple(evidence_records)
    if (
        transcript_manifest.job_id != job_id
        or evidence_manifest.job_id != job_id
        or transcript_manifest.segment_count != len(segment_values)
        or evidence_manifest.evidence_count != len(evidence_values)
    ):
        raise _index_invalid("search source manifests do not match the job or record counts")

    segment_ids = [item.segment_id for item in segment_values]
    evidence_ids = [item.evidence_id for item in evidence_values]
    if segment_ids != [f"T{index:06d}" for index in range(1, len(segment_ids) + 1)]:
        raise _index_invalid("search transcript IDs are not canonical")
    if evidence_ids != [f"E{index:06d}" for index in range(1, len(evidence_ids) + 1)]:
        raise _index_invalid("search evidence IDs are not canonical")

    _verify_canonical_artifact(
        segment_values,
        expected_size=transcript_manifest.segments_artifact.size_bytes,
        expected_sha256=transcript_manifest.segments_artifact.content_sha256,
    )
    _verify_canonical_artifact(
        evidence_values,
        expected_size=evidence_manifest.evidence_artifact.size_bytes,
        expected_sha256=evidence_manifest.evidence_artifact.content_sha256,
    )

    documents: list[_Document] = []
    indexed_utf8_bytes = 0
    for segment in segment_values:
        content = _normalizer(segment.normalized_text)
        if not content or content != segment.normalized_text or len(content) > 65536:
            raise _index_invalid("search transcript text is not canonically normalized")
        indexed_utf8_bytes += len(content.encode("utf-8"))
        if indexed_utf8_bytes > MAX_INDEXED_UTF8_BYTES:
            raise integrity_error(
                "INDEX_INPUT_TOO_LARGE", "search source text exceeds the indexing budget"
            )
        documents.append(
            _Document(
                rowid=_rowid(SearchDocumentKind.TRANSCRIPT, segment.segment_id),
                kind=SearchDocumentKind.TRANSCRIPT,
                record_id=segment.segment_id,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                content=content,
                content_sha256=sha256_bytes(content.encode("utf-8")),
            )
        )
    for record in evidence_values:
        content = _normalizer(record.observation)
        if not content or len(content) > 65536:
            raise _index_invalid("search evidence observation is invalid")
        indexed_utf8_bytes += len(content.encode("utf-8"))
        if indexed_utf8_bytes > MAX_INDEXED_UTF8_BYTES:
            raise integrity_error(
                "INDEX_INPUT_TOO_LARGE", "search source text exceeds the indexing budget"
            )
        documents.append(
            _Document(
                rowid=_rowid(SearchDocumentKind.EVIDENCE, record.evidence_id),
                kind=SearchDocumentKind.EVIDENCE,
                record_id=record.evidence_id,
                start_ms=None,
                end_ms=None,
                content=content,
                content_sha256=sha256_bytes(content.encode("utf-8")),
            )
        )
    return tuple(documents)


def _documents_fingerprint(documents: Sequence[_Document]) -> str:
    digest = hashlib.sha256()
    for document in documents:
        digest.update(canonical_json_bytes(document.fingerprint_value()))
        digest.update(b"\n")
    return digest.hexdigest()


def _semantic_value(
    *,
    job_id: str,
    transcript_manifest: TranscriptManifest,
    evidence_manifest: EvidenceManifest,
    sqlite_version: str,
    documents_fingerprint: str,
    indexed_utf8_bytes: int,
) -> dict[str, object]:
    return {
        "metadata_schema_version": "1.0",
        "job_id": job_id,
        "transcript_segments_artifact": transcript_manifest.segments_artifact.model_dump(
            mode="json"
        ),
        "evidence_artifact": evidence_manifest.evidence_artifact.model_dump(mode="json"),
        "transcription_fingerprint": transcript_manifest.transcription_fingerprint,
        "transcript_bundle_fingerprint": evidence_manifest.transcript_bundle_fingerprint,
        "evidence_bundle_fingerprint": evidence_manifest.evidence_bundle_fingerprint,
        "documents_fingerprint": documents_fingerprint,
        "indexed_utf8_bytes": indexed_utf8_bytes,
        "query_version": SEARCH_QUERY_VERSION,
        "builder_version": SEARCH_BUILDER_VERSION,
        "implementation_version": SEARCH_IMPLEMENTATION_VERSION,
        "tokenizer": SEARCH_TOKENIZER,
        "normalizer": SEARCH_NORMALIZER,
        "sqlite_version": sqlite_version,
        "sqlite_capability": SQLITE_CAPABILITY,
        "fts5_available": True,
        "trigram_available": True,
        "short_query_scan_limit_per_kind": SHORT_QUERY_SCAN_LIMIT_PER_KIND,
        "maximum_indexed_utf8_bytes": MAX_INDEXED_UTF8_BYTES,
        "transcript_count": transcript_manifest.segment_count,
        "evidence_count": evidence_manifest.evidence_count,
    }


def _metadata_semantic_value(metadata: SearchIndexMetadata) -> dict[str, object]:
    return {
        "metadata_schema_version": metadata.schema_version,
        "job_id": metadata.job_id,
        "transcript_segments_artifact": metadata.transcript_segments_artifact.model_dump(
            mode="json"
        ),
        "evidence_artifact": metadata.evidence_artifact.model_dump(mode="json"),
        "transcription_fingerprint": metadata.transcription_fingerprint,
        "transcript_bundle_fingerprint": metadata.transcript_bundle_fingerprint,
        "evidence_bundle_fingerprint": metadata.evidence_bundle_fingerprint,
        "documents_fingerprint": metadata.documents_fingerprint,
        "indexed_utf8_bytes": metadata.indexed_utf8_bytes,
        "query_version": metadata.query_version,
        "builder_version": metadata.builder_version,
        "implementation_version": metadata.implementation_version,
        "tokenizer": metadata.tokenizer,
        "normalizer": metadata.normalizer,
        "sqlite_version": metadata.sqlite_version,
        "sqlite_capability": metadata.sqlite_capability,
        "fts5_available": metadata.fts5_available,
        "trigram_available": metadata.trigram_available,
        "short_query_scan_limit_per_kind": metadata.short_query_scan_limit_per_kind,
        "maximum_indexed_utf8_bytes": metadata.maximum_indexed_utf8_bytes,
        "transcript_count": metadata.transcript_count,
        "evidence_count": metadata.evidence_count,
    }


def _expected_semantic_fingerprint(
    job_id: str,
    transcript_manifest: TranscriptManifest,
    evidence_manifest: EvidenceManifest,
    documents: Sequence[_Document],
) -> str:
    if transcript_manifest.job_id != job_id or evidence_manifest.job_id != job_id:
        raise _index_invalid("search source manifests do not match the job")
    return fingerprint(
        _semantic_value(
            job_id=job_id,
            transcript_manifest=transcript_manifest,
            evidence_manifest=evidence_manifest,
            sqlite_version=sqlite3.sqlite_version,
            documents_fingerprint=_documents_fingerprint(documents),
            indexed_utf8_bytes=sum(len(item.content.encode("utf-8")) for item in documents),
        )
    )


def _new_metadata(
    job_id: str,
    transcript_manifest: TranscriptManifest,
    evidence_manifest: EvidenceManifest,
    documents: Sequence[_Document],
) -> SearchIndexMetadata:
    semantic_fingerprint = _expected_semantic_fingerprint(
        job_id, transcript_manifest, evidence_manifest, documents
    )
    try:
        return SearchIndexMetadata(
            job_id=job_id,
            transcript_segments_artifact=transcript_manifest.segments_artifact,
            evidence_artifact=evidence_manifest.evidence_artifact,
            transcription_fingerprint=transcript_manifest.transcription_fingerprint,
            transcript_bundle_fingerprint=evidence_manifest.transcript_bundle_fingerprint,
            evidence_bundle_fingerprint=evidence_manifest.evidence_bundle_fingerprint,
            semantic_fingerprint=semantic_fingerprint,
            documents_fingerprint=_documents_fingerprint(documents),
            implementation_version=SEARCH_IMPLEMENTATION_VERSION,
            sqlite_version=sqlite3.sqlite_version,
            indexed_utf8_bytes=sum(len(item.content.encode("utf-8")) for item in documents),
            transcript_count=transcript_manifest.segment_count,
            evidence_count=evidence_manifest.evidence_count,
            built_at=datetime.now(UTC),
        )
    except ValidationError:
        raise _index_invalid("search index metadata could not be constructed") from None


def _is_fts_unavailable(error: sqlite3.Error) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "no such module: fts5",
            "no such tokenizer",
            "error in tokenizer constructor",
            "unknown tokenizer",
        )
    )


def _raise_database_error(error: sqlite3.Error, *, message: str) -> None:
    if _is_fts_unavailable(error):
        raise integrity_error(
            "FTS5_TRIGRAM_UNAVAILABLE",
            "SQLite FTS5 with the trigram tokenizer is required",
        ) from None
    raise _index_invalid(message) from None


def _configure_writable(connection: sqlite3.Connection, *, file_backed: bool) -> None:
    try:
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != (
            "delete" if file_backed else "memory"
        ):
            raise _index_invalid("search index journal mode is invalid")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA busy_timeout=250")
        values = (
            connection.execute("PRAGMA synchronous").fetchone(),
            connection.execute("PRAGMA foreign_keys").fetchone(),
            connection.execute("PRAGMA trusted_schema").fetchone(),
            connection.execute("PRAGMA temp_store").fetchone(),
            connection.execute("PRAGMA mmap_size").fetchone(),
        )
        expected_mmap = (0,) if file_backed else None
        if values != ((2,), (1,), (0,), (2,), expected_mmap):
            raise _index_invalid("search index SQLite safety pragmas are invalid")
    except sqlite3.Error as error:
        _raise_database_error(error, message="search index SQLite configuration failed")


def _configure_readonly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA busy_timeout=250")
        values = (
            connection.execute("PRAGMA query_only").fetchone(),
            connection.execute("PRAGMA foreign_keys").fetchone(),
            connection.execute("PRAGMA trusted_schema").fetchone(),
            connection.execute("PRAGMA temp_store").fetchone(),
            connection.execute("PRAGMA mmap_size").fetchone(),
        )
        if values != ((1,), (1,), (0,), (2,), (0,)):
            raise _index_invalid("search index read-only pragmas are invalid")
    except sqlite3.Error as error:
        _raise_database_error(error, message="search index read-only configuration failed")


def _require_fts5_trigram(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.__vep_fts5_trigram_probe "
            "USING fts5(content, tokenize='trigram')"
        )
        connection.execute("DROP TABLE temp.__vep_fts5_trigram_probe")
    except sqlite3.Error as error:
        if _is_fts_unavailable(error):
            _raise_database_error(error, message="SQLite FTS5 trigram capability is unavailable")
        raise _index_invalid("SQLite FTS5 trigram capability probe failed") from None


def _fts_integrity_check(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            "INSERT INTO documents_fts(documents_fts, rank) VALUES('integrity-check', 1)"
        )
    except sqlite3.Error as error:
        _raise_database_error(error, message="search FTS index integrity check failed")


def _database_integrity_check(connection: sqlite3.Connection) -> None:
    try:
        result = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as error:
        _raise_database_error(error, message="search database integrity check failed")
    if result != [("ok",)]:
        raise _index_invalid("search database integrity check failed")


def _fts_integrity_check_on_private_copy(connection: sqlite3.Connection, source_path: Path) -> None:
    nested_staging = source_path.parent / "staging"
    verification_directory = nested_staging if nested_staging.is_dir() else source_path.parent
    ensure_private_directory(verification_directory)
    copied_path = verification_directory / f"{uuid.uuid4().hex}.sqlite3"
    _create_private_empty_file(copied_path)
    copied: sqlite3.Connection | None = None
    try:
        try:
            copied = sqlite3.connect(copied_path, timeout=0.25, isolation_level=None)
            connection.backup(copied)
            copied.close()
            copied = sqlite3.connect(copied_path, timeout=0.25, isolation_level=None)
            _configure_writable(copied, file_backed=True)
            _fts_integrity_check(copied)
        except sqlite3.Error as error:
            _raise_database_error(error, message="search verification copy could not be created")
    finally:
        if copied is not None:
            copied.close()
        _cleanup_staging(copied_path)


def _build_connection(
    connection: sqlite3.Connection,
    documents: Sequence[_Document],
    metadata: SearchIndexMetadata,
    *,
    file_backed: bool,
) -> None:
    _configure_writable(connection, file_backed=file_backed)
    _require_fts5_trigram(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CREATE_DOCUMENTS_SQL)
        connection.execute(_CREATE_METADATA_SQL)
        connection.execute(_CREATE_FTS_SQL)
        connection.executemany(
            _INSERT_DOCUMENT_SQL, (document.database_values() for document in documents)
        )
        connection.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
        _fts_integrity_check(connection)
        connection.execute(
            """
            INSERT INTO index_metadata(singleton, metadata_json, semantic_fingerprint, complete)
            VALUES (1, ?, ?, 1)
            """,
            (
                canonical_json_bytes(metadata).decode("utf-8"),
                metadata.semantic_fingerprint,
            ),
        )
        connection.execute("COMMIT")
    except VepError:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        _raise_database_error(error, message="search index build failed")
    _database_integrity_check(connection)
    _fts_integrity_check(connection)


def _schema_is_valid(connection: sqlite3.Connection) -> bool:
    table_rows = connection.execute("PRAGMA table_list").fetchall()
    main_tables = {row[1]: row for row in table_rows if row[0] == "main"}
    allowed = {
        "documents",
        "index_metadata",
        "documents_fts",
        "documents_fts_config",
        "documents_fts_data",
        "documents_fts_docsize",
        "documents_fts_idx",
        "sqlite_schema",
    }
    if set(main_tables) != allowed:
        return False
    if main_tables["documents"][2] != "table" or main_tables["documents"][5] != 1:
        return False
    if main_tables["index_metadata"][2] != "table" or main_tables["index_metadata"][5] != 1:
        return False
    if main_tables["documents_fts"][2] != "virtual":
        return False
    fts_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'documents_fts'"
    ).fetchone()
    if fts_sql_row is None or not isinstance(fts_sql_row[0], str):
        return False
    compact_fts_sql = "".join(fts_sql_row[0].casefold().split())
    if not all(
        value in compact_fts_sql
        for value in (
            "usingfts5(",
            "content='documents'",
            "content_rowid='rowid'",
            "tokenize='trigram'",
        )
    ):
        return False
    forbidden = connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('view', 'trigger')"
    ).fetchone()
    return forbidden == (0,)


def _read_metadata(connection: sqlite3.Connection) -> SearchIndexMetadata:
    try:
        rows = connection.execute(
            """
            SELECT metadata_json, semantic_fingerprint, complete
            FROM index_metadata
            ORDER BY singleton
            """
        ).fetchall()
        if len(rows) != 1 or rows[0][2] != 1:
            raise _index_invalid("search index completion metadata is invalid")
        metadata = SearchIndexMetadata.model_validate_json(rows[0][0], strict=True)
    except ValidationError:
        raise _index_invalid("search index metadata is malformed") from None
    except sqlite3.Error as error:
        _raise_database_error(error, message="search index metadata is unavailable")
    if rows[0][1] != metadata.semantic_fingerprint or not metadata.complete:
        raise _index_invalid("search index metadata is inconsistent")
    if fingerprint(_metadata_semantic_value(metadata)) != metadata.semantic_fingerprint:
        raise _index_invalid("search index semantic metadata is inconsistent")
    return metadata


def _document_from_row(row: tuple[object, ...]) -> _Document:
    if len(row) < 7:
        raise _index_invalid("search document row is malformed")
    rowid, kind_value, record_id, start_ms, end_ms, content, content_sha256 = row[:7]
    if (
        type(rowid) is not int
        or not isinstance(kind_value, str)
        or not isinstance(record_id, str)
        or not isinstance(content, str)
        or not isinstance(content_sha256, str)
        or (start_ms is not None and type(start_ms) is not int)
        or (end_ms is not None and type(end_ms) is not int)
    ):
        raise _index_invalid("search document row has invalid SQLite types")
    try:
        kind = SearchDocumentKind(kind_value)
    except ValueError:
        raise _index_invalid("search document kind is invalid") from None
    document = _Document(
        rowid=rowid,
        kind=kind,
        record_id=record_id,
        start_ms=start_ms,
        end_ms=end_ms,
        content=content,
        content_sha256=content_sha256,
    )
    if (
        document.rowid != _rowid(document.kind, document.record_id)
        or not document.content
        or len(document.content) > 65536
        or _normalizer(document.content) != document.content
        or sha256_bytes(document.content.encode("utf-8")) != document.content_sha256
    ):
        raise _index_invalid("search document content or identity is invalid")
    if document.kind is SearchDocumentKind.TRANSCRIPT:
        if (document.start_ms is None) != (document.end_ms is None) or (
            document.start_ms is not None
            and document.end_ms is not None
            and (document.start_ms < 0 or document.end_ms <= document.start_ms)
        ):
            raise _index_invalid("search transcript interval is invalid")
    elif document.start_ms is not None or document.end_ms is not None:
        raise _index_invalid("search evidence document contains an inferred interval")
    return document


def _verify_documents(
    connection: sqlite3.Connection,
    metadata: SearchIndexMetadata,
    *,
    full: bool,
) -> None:
    counts = dict(
        connection.execute("SELECT kind, count(*) FROM documents GROUP BY kind").fetchall()
    )
    if (
        counts.get(SearchDocumentKind.TRANSCRIPT.value, 0) != metadata.transcript_count
        or counts.get(SearchDocumentKind.EVIDENCE.value, 0) != metadata.evidence_count
        or set(counts)
        - {
            SearchDocumentKind.TRANSCRIPT.value,
            SearchDocumentKind.EVIDENCE.value,
        }
    ):
        raise _index_invalid("search index document counts are inconsistent")
    fts_count = connection.execute("SELECT count(*) FROM documents_fts").fetchone()
    if fts_count != (metadata.transcript_count + metadata.evidence_count,):
        raise _index_invalid("search FTS document count is inconsistent")
    if not full:
        return
    cursor = connection.execute(
        """
        SELECT rowid, kind, record_id, start_ms, end_ms, content, content_sha256
        FROM documents
        ORDER BY rowid
        """
    )
    try:
        documents = tuple(_document_from_row(row) for row in cursor)
    finally:
        cursor.close()
    if _documents_fingerprint(documents) != metadata.documents_fingerprint:
        raise _index_invalid("search index document fingerprint is inconsistent")
    indexed_utf8_bytes = sum(len(item.content.encode("utf-8")) for item in documents)
    if (
        indexed_utf8_bytes != metadata.indexed_utf8_bytes
        or indexed_utf8_bytes > metadata.maximum_indexed_utf8_bytes
    ):
        raise _index_invalid("search index text budget metadata is inconsistent")


def _verify_connection(
    connection: sqlite3.Connection,
    *,
    expected_semantic_fingerprint: str,
    full: bool,
    source_path: Path | None = None,
) -> SearchIndexMetadata:
    if full:
        _database_integrity_check(connection)
    try:
        if not _schema_is_valid(connection):
            raise _index_invalid("search index schema is invalid")
        metadata = _read_metadata(connection)
        if metadata.semantic_fingerprint != expected_semantic_fingerprint:
            raise integrity_error("INDEX_STALE", "search index is stale and must be rebuilt")
        _verify_documents(connection, metadata, full=full)
        connection.execute("SELECT rowid FROM documents_fts LIMIT 0").fetchall()
        if full:
            if source_path is None:
                raise _index_invalid("search verification source path is unavailable")
            _fts_integrity_check_on_private_copy(connection, source_path)
        return metadata
    except VepError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error, message="search index verification failed")


def _sidecar_paths(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in _SIDECAR_SUFFIXES)


def _assert_no_sidecars(path: Path) -> None:
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in _sidecar_paths(path)):
        raise _index_invalid("search index contains a forbidden SQLite sidecar")


def _validate_index_file(path: Path) -> None:
    try:
        is_junction = getattr(path, "is_junction", lambda: False)
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise _index_invalid("search index is unavailable") from None
    if (
        path.is_symlink()
        or is_junction()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_INDEX_BYTES
    ):
        raise _index_invalid("search index file shape is invalid")
    _assert_no_sidecars(path)


def _open_readonly(path: Path) -> sqlite3.Connection:
    _validate_index_file(path)
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.25, isolation_level=None)
    except sqlite3.Error as error:
        _raise_database_error(error, message="search index could not be opened read-only")
    try:
        _configure_readonly(connection)
        return connection
    except Exception:
        connection.close()
        raise


def _verify_index_file(
    path: Path,
    *,
    expected_semantic_fingerprint: str,
    full: bool,
) -> SearchIndexMetadata:
    connection = _open_readonly(path)
    try:
        return _verify_connection(
            connection,
            expected_semantic_fingerprint=expected_semantic_fingerprint,
            full=full,
            source_path=path,
        )
    finally:
        connection.close()


def _create_private_empty_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        enforce_private_descriptor(descriptor, PRIVATE_FILE_MODE)
    except OSError as error:
        raise io_error(
            "SEARCH_INDEX_BUILD_FAILED", "search staging database could not be created"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        os.fsync(descriptor)
    except OSError as error:
        raise io_error(
            "SEARCH_INDEX_BUILD_FAILED", "search index could not be synchronized"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _cleanup_staging(path: Path) -> None:
    for candidate in (path, *_sidecar_paths(path)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            raise io_error(
                "SEARCH_INDEX_BUILD_FAILED", "search staging database could not be removed"
            ) from None


def _cleanup_stale_staging(directory: Path) -> None:
    try:
        candidates = tuple(directory.iterdir())
    except OSError:
        raise _index_invalid("search staging directory is unavailable") from None
    for candidate in candidates:
        try:
            metadata = candidate.stat(follow_symlinks=False)
            is_junction = getattr(candidate, "is_junction", lambda: False)
            if (
                candidate.parent != directory
                or _STAGING_FILE_NAME.fullmatch(candidate.name) is None
                or candidate.is_symlink()
                or is_junction()
                or candidate.is_mount()
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise _index_invalid("search staging directory contains an invalid entry")
            candidate.unlink()
        except VepError:
            raise
        except OSError:
            raise _index_invalid("search staging residue could not be removed") from None


def verify_index_rebuildable(
    job_id: str,
    transcript_manifest: TranscriptManifest,
    segments: Sequence[TranscriptSegment],
    evidence_manifest: EvidenceManifest,
    evidence_records: Sequence[EvidenceRecord],
) -> SearchIndexMetadata:
    """Build and integrity-check the canonical index in bounded private file storage."""
    documents = _validate_sources(
        job_id, transcript_manifest, segments, evidence_manifest, evidence_records
    )
    metadata = _new_metadata(job_id, transcript_manifest, evidence_manifest, documents)
    with tempfile.TemporaryDirectory(prefix="vep-search-verify-") as temporary:
        directory = Path(temporary)
        ensure_private_directory(directory)
        staging = directory / f"{uuid.uuid4().hex}.sqlite3"
        _create_private_empty_file(staging)
        try:
            try:
                connection = sqlite3.connect(staging, timeout=0.25, isolation_level=None)
            except sqlite3.Error as error:
                _raise_database_error(
                    error, message="search verification index could not be opened"
                )
            try:
                _build_connection(connection, documents, metadata, file_backed=True)
            finally:
                connection.close()
            _assert_no_sidecars(staging)
            if staging.stat(follow_symlinks=False).st_size > MAX_INDEX_BYTES:
                raise integrity_error(
                    "INDEX_INPUT_TOO_LARGE", "rebuilt search index exceeds its storage budget"
                )
            _verify_index_file(
                staging,
                expected_semantic_fingerprint=metadata.semantic_fingerprint,
                full=True,
            )
            return metadata
        finally:
            _cleanup_staging(staging)


def build_index(
    workspace: JobWorkspace,
    job_id: str,
    transcript_manifest: TranscriptManifest,
    segments: Sequence[TranscriptSegment],
    evidence_manifest: EvidenceManifest,
    evidence_records: Sequence[EvidenceRecord],
) -> SearchIndexBuildResult:
    """Build, verify, and atomically publish a private rebuildable search database."""
    documents = _validate_sources(
        job_id, transcript_manifest, segments, evidence_manifest, evidence_records
    )
    metadata = _new_metadata(job_id, transcript_manifest, evidence_manifest, documents)
    expected = metadata.semantic_fingerprint
    target = workspace.search_index_sqlite
    search_directory = workspace.safe_path("search")
    staging_directory = workspace.safe_path("search/staging")
    ensure_private_directory(search_directory)
    ensure_private_directory(staging_directory)

    with exclusive_file_lock(
        workspace.search_lock_path,
        busy_code="SEARCH_INDEX_BUSY",
        busy_message="search index is being built by another process",
        failure_code="SEARCH_INDEX_LOCK_FAILED",
        failure_message="search index lock could not be acquired",
    ):
        _cleanup_stale_staging(staging_directory)
        if target.exists():
            try:
                existing = _verify_index_file(
                    target, expected_semantic_fingerprint=expected, full=True
                )
            except VepError as error:
                if error.code not in {"INDEX_INVALID", "INDEX_STALE"}:
                    raise
            else:
                return SearchIndexBuildResult(metadata=existing, reused=True)

        staging = workspace.safe_path(f"search/staging/{uuid.uuid4().hex}.sqlite3")
        _create_private_empty_file(staging)
        published = False
        try:
            try:
                connection = sqlite3.connect(staging, timeout=0.25, isolation_level=None)
            except sqlite3.Error as error:
                _raise_database_error(error, message="search staging database could not be opened")
            try:
                _build_connection(connection, documents, metadata, file_backed=True)
            finally:
                connection.close()
            _assert_no_sidecars(staging)
            if staging.stat(follow_symlinks=False).st_size > MAX_INDEX_BYTES:
                raise integrity_error(
                    "INDEX_INPUT_TOO_LARGE", "search index exceeds its storage budget"
                )
            enforce_private_file(staging)
            _fsync_file(staging)
            _verify_index_file(staging, expected_semantic_fingerprint=expected, full=True)
            _assert_no_sidecars(target)
            try:
                os.replace(staging, target)
            except OSError as error:
                raise io_error(
                    "SEARCH_INDEX_BUILD_FAILED", "search index could not be published atomically"
                ) from error
            published = True
            enforce_private_file(target)
            _fsync_file(target)
            published_metadata = _verify_index_file(
                target, expected_semantic_fingerprint=expected, full=True
            )
            return SearchIndexBuildResult(metadata=published_metadata, reused=False)
        finally:
            if not published:
                _cleanup_staging(staging)


def _normalize_query(query: object, limit: object) -> tuple[str, int]:
    if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise integrity_error(
            "SEARCH_QUERY_INVALID", "search query and limit must satisfy their bounds"
        )
    try:
        raw_size = len(query.encode("utf-8"))
        normalized = _normalizer(query)
        normalized_size = len(normalized.encode("utf-8"))
    except (UnicodeError, ValueError):
        raise integrity_error(
            "SEARCH_QUERY_INVALID", "search query contains invalid Unicode"
        ) from None
    if (
        not normalized
        or "\x00" in query
        or len(query) > MAX_QUERY_CHARS
        or len(normalized) > MAX_QUERY_CHARS
        or raw_size > MAX_QUERY_UTF8_BYTES
        or normalized_size > MAX_QUERY_UTF8_BYTES
    ):
        raise integrity_error("SEARCH_QUERY_INVALID", "search query exceeds its safe bounds")
    return normalized, limit


def _literal_match_expression(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def _query_width(query: str) -> int:
    return len(query.replace(" ", ""))


def _search_rows(
    connection: sqlite3.Connection,
    normalized_query: str,
    limit: int,
) -> tuple[SearchQueryMode, int | None, list[tuple[object, ...]]]:
    try:
        if _query_width(normalized_query) >= 3:
            rows = connection.execute(
                _FTS_SEARCH_SQL,
                (_literal_match_expression(normalized_query), limit + 1),
            ).fetchall()
            return SearchQueryMode.FTS5_LITERAL_MATCH, None, rows
        rows = connection.execute(
            _SHORT_SEARCH_SQL,
            (
                SearchDocumentKind.TRANSCRIPT.value,
                SHORT_QUERY_SCAN_LIMIT_PER_KIND,
                SearchDocumentKind.EVIDENCE.value,
                SHORT_QUERY_SCAN_LIMIT_PER_KIND,
                normalized_query,
                limit + 1,
            ),
        ).fetchall()
        return (
            SearchQueryMode.BOUNDED_LITERAL_SCAN,
            SHORT_QUERY_SCAN_LIMIT_PER_KIND,
            rows,
        )
    except sqlite3.Error as error:
        _raise_database_error(error, message="search index query failed")


def _hits_from_rows(rows: Sequence[tuple[object, ...]], limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for rank, row in enumerate(rows[:limit], start=1):
        document = _document_from_row(row)
        score = row[7]
        if score is not None and not isinstance(score, (float, int)):
            raise _index_invalid("search rank value is invalid")
        try:
            hits.append(
                SearchHit(
                    rank=rank,
                    kind=document.kind,
                    record_id=document.record_id,
                    start_ms=document.start_ms,
                    end_ms=document.end_ms,
                    content=document.content,
                    score=float(score) if score is not None else None,
                )
            )
        except ValidationError:
            raise _index_invalid("search result row is invalid") from None
    return hits


def search_index(
    workspace: JobWorkspace,
    job_id: str,
    transcript_manifest: TranscriptManifest,
    segments: Sequence[TranscriptSegment],
    evidence_manifest: EvidenceManifest,
    evidence_records: Sequence[EvidenceRecord],
    query: str,
    limit: int,
) -> SearchResult:
    """Search a verified current index without creating SQLite sidecars."""
    normalized_query, bounded_limit = _normalize_query(query, limit)
    documents = _validate_sources(
        job_id, transcript_manifest, segments, evidence_manifest, evidence_records
    )
    expected = _expected_semantic_fingerprint(
        job_id, transcript_manifest, evidence_manifest, documents
    )
    with exclusive_file_lock(
        workspace.search_lock_path,
        busy_code="SEARCH_INDEX_BUSY",
        busy_message="search index is being built or verified by another process",
        failure_code="SEARCH_INDEX_LOCK_FAILED",
        failure_message="search index lock could not be acquired",
    ):
        connection = _open_readonly(workspace.search_index_sqlite)
        try:
            metadata = _verify_connection(
                connection,
                expected_semantic_fingerprint=expected,
                full=True,
                source_path=workspace.search_index_sqlite,
            )
            mode, scan_limit, rows = _search_rows(connection, normalized_query, bounded_limit)
            hits = _hits_from_rows(rows, bounded_limit)
            potentially_unscanned = mode is SearchQueryMode.BOUNDED_LITERAL_SCAN and (
                metadata.transcript_count > SHORT_QUERY_SCAN_LIMIT_PER_KIND
                or metadata.evidence_count > SHORT_QUERY_SCAN_LIMIT_PER_KIND
            )
            return SearchResult(
                metadata=metadata,
                query=query,
                normalized_query=normalized_query,
                mode=mode,
                limit=bounded_limit,
                scan_limit_per_kind=scan_limit,
                truncated=len(rows) > bounded_limit or potentially_unscanned,
                hits=hits,
            )
        except ValidationError:
            raise _index_invalid("search result could not be validated") from None
        finally:
            connection.close()
