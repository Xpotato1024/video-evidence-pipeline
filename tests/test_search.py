from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

import video_evidence_pipeline.search as search_module
from video_evidence_pipeline.domain.artifacts import ArtifactRef, EvidenceLedgerArtifactRef
from video_evidence_pipeline.domain.evidence import (
    AnalyzerExecutionMode,
    AnalyzerIdentity,
    EvidenceConfidence,
    EvidenceManifest,
    EvidenceRecord,
    InterpretationLevel,
    SourceRef,
    SourceRefKind,
)
from video_evidence_pipeline.domain.hashing import sha256_bytes
from video_evidence_pipeline.domain.search import SearchQueryMode
from video_evidence_pipeline.domain.serialization import canonical_json_bytes, fingerprint
from video_evidence_pipeline.domain.transcription import (
    SegmentLineage,
    TranscriptConfidence,
    TranscriptConfidenceKind,
    TranscriptionBackendIdentity,
    TranscriptionConfig,
    TranscriptManifest,
    TranscriptSegment,
    TranscriptSource,
)
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.search import (
    SHORT_QUERY_SCAN_LIMIT_PER_KIND,
    build_index,
    search_index,
    verify_index_rebuildable,
)
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.locking import exclusive_file_lock


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _artifact(
    records: tuple[TranscriptSegment | EvidenceRecord, ...],
    *,
    artifact_id: str,
    artifact_type: str,
    path: str,
) -> ArtifactRef:
    payload = b"".join(canonical_json_bytes(item) + b"\n" for item in records)
    artifact_model = (
        EvidenceLedgerArtifactRef if artifact_type == "evidence_records" else ArtifactRef
    )
    return artifact_model(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        content_sha256=sha256_bytes(payload),
        size_bytes=len(payload),
        relative_storage_path=path,
    )


def _bundle(
    *,
    transcript_texts: tuple[str, ...] = ("  これは　日本語の原文です  ",),
    evidence_texts: tuple[str, ...] = ("日本語の説明が映像内で明示された。",),
) -> tuple[
    TranscriptManifest,
    tuple[TranscriptSegment, ...],
    EvidenceManifest,
    tuple[EvidenceRecord, ...],
]:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    source_artifact = ArtifactRef(
        artifact_id="speech-audio",
        artifact_type="speech_audio",
        content_sha256="a" * 64,
        size_bytes=123,
        relative_storage_path="normalized/audio/speech.flac",
    )
    backend = TranscriptionBackendIdentity(
        source=TranscriptSource.ASR,
        name="synthetic-asr",
        version="1.0",
        runtime="test",
        model="synthetic",
        model_fingerprint="b" * 64,
        local_files_only=True,
    )
    segments = tuple(
        TranscriptSegment(
            segment_id=f"T{index:06d}",
            start_ms=(index - 1) * 1000,
            end_ms=index * 1000,
            raw_text=text,
            normalized_text=_normalize(text),
            source=TranscriptSource.ASR,
            confidence=TranscriptConfidence(
                kind=TranscriptConfidenceKind.BACKEND_METRIC,
                avg_logprob=-0.25,
                no_speech_probability=0.01,
            ),
            flags=["raw-only-marker"],
            lineage=SegmentLineage(
                source_artifact=source_artifact,
                source_record_id=f"asr-{index}",
                backend_identity_fingerprint=fingerprint(backend),
            ),
        )
        for index, text in enumerate(transcript_texts, start=1)
    )
    transcript_artifact = _artifact(
        segments,
        artifact_id="transcript-segments",
        artifact_type="transcript_segments",
        path="transcript/segments/transcript.jsonl",
    )
    transcript_manifest = TranscriptManifest(
        job_id="demo-search",
        transcription_fingerprint=sha256_bytes(
            ("transcription:" + transcript_artifact.content_sha256).encode()
        ),
        media_bundle_fingerprint="c" * 64,
        implementation_version="transcription-v1",
        configuration=TranscriptionConfig(),
        backends=[backend],
        selected_source_artifacts=[source_artifact],
        segments_artifact=transcript_artifact,
        segment_count=len(segments),
        duration_ms=len(segments) * 1000,
        started_at=now,
        created_at=now,
    )

    evidence_records = tuple(
        EvidenceRecord(
            evidence_id=f"E{index:06d}",
            observation=text,
            interpretation_level=InterpretationLevel.EXPLICIT_STATEMENT,
            confidence=EvidenceConfidence.HIGH,
            source_refs=[
                SourceRef(
                    kind=SourceRefKind.TRANSCRIPT_SEGMENT,
                    ref_id=segments[min(index - 1, len(segments) - 1)].segment_id,
                    start_ms=segments[min(index - 1, len(segments) - 1)].start_ms,
                    end_ms=segments[min(index - 1, len(segments) - 1)].end_ms,
                    content_sha256=fingerprint(segments[min(index - 1, len(segments) - 1)]),
                )
            ],
            asserted_as_video_fact=True,
            flags=["evidence-private-flag"],
        )
        for index, text in enumerate(evidence_texts, start=1)
    )
    evidence_artifact = _artifact(
        evidence_records,
        artifact_id="evidence-records",
        artifact_type="evidence_records",
        path="evidence/records/evidence.jsonl",
    )
    ambiguity_payload = b"[]\n"
    response_payload = b"{}\n"
    evidence_manifest = EvidenceManifest(
        job_id="demo-search",
        evidence_bundle_fingerprint=sha256_bytes(
            ("evidence:" + evidence_artifact.content_sha256).encode()
        ),
        transcript_bundle_fingerprint=transcript_manifest.transcription_fingerprint,
        media_bundle_fingerprint="c" * 64,
        request_fingerprint="e" * 64,
        response_fingerprint="f" * 64,
        response_created_at=now,
        implementation_version="evidence-v1",
        analyzer=AnalyzerIdentity(
            provider="synthetic",
            model="fixture",
            version="1.0",
            executor="pytest",
            execution_mode=AnalyzerExecutionMode.TEST_FIXTURE,
            external_api_used=False,
        ),
        response_artifact=ArtifactRef(
            artifact_id="evidence-response",
            artifact_type="evidence_response",
            content_sha256=sha256_bytes(response_payload),
            size_bytes=len(response_payload),
            relative_storage_path="evidence/records/response.json",
        ),
        evidence_artifact=evidence_artifact,
        ambiguity_artifact=ArtifactRef(
            artifact_id="ambiguity-records",
            artifact_type="ambiguity_records",
            content_sha256=sha256_bytes(ambiguity_payload),
            size_bytes=len(ambiguity_payload),
            relative_storage_path="evidence/records/ambiguity.jsonl",
        ),
        evidence_count=len(evidence_records),
        ambiguity_count=0,
        started_at=now,
        created_at=now,
    )
    return transcript_manifest, segments, evidence_manifest, evidence_records


def _build(
    tmp_path: Path,
    *,
    transcript_texts: tuple[str, ...] = ("  これは　日本語の原文です  ",),
    evidence_texts: tuple[str, ...] = ("日本語の説明が映像内で明示された。",),
) -> tuple[
    JobWorkspace,
    TranscriptManifest,
    tuple[TranscriptSegment, ...],
    EvidenceManifest,
    tuple[EvidenceRecord, ...],
]:
    workspace = JobWorkspace.create(tmp_path, "demo-search")
    transcript_manifest, segments, evidence_manifest, evidence_records = _bundle(
        transcript_texts=transcript_texts,
        evidence_texts=evidence_texts,
    )
    build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )
    return workspace, transcript_manifest, segments, evidence_manifest, evidence_records


def test_builds_private_strict_external_content_index_with_stable_rowids(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(tmp_path, "demo-search")
    transcript_manifest, segments, evidence_manifest, evidence_records = _bundle()
    manifests_before = set(workspace.safe_path("manifests").rglob("*"))

    verified = verify_index_rebuildable(
        "demo-search", transcript_manifest, segments, evidence_manifest, evidence_records
    )
    assert not workspace.search_index_sqlite.exists()
    built = build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )

    assert built.reused is False
    assert built.metadata.semantic_fingerprint == verified.semantic_fingerprint
    assert built.metadata.complete is True
    assert built.metadata.transcript_segments_artifact == transcript_manifest.segments_artifact
    assert built.metadata.evidence_artifact == evidence_manifest.evidence_artifact
    assert built.metadata.fts5_available is True
    assert built.metadata.trigram_available is True
    assert built.metadata.short_query_scan_limit_per_kind == SHORT_QUERY_SCAN_LIMIT_PER_KIND
    assert built.metadata.indexed_utf8_bytes > 0
    assert built.metadata.maximum_indexed_utf8_bytes == 64 * 1024 * 1024
    assert set(workspace.safe_path("manifests").rglob("*")) == manifests_before
    assert not any(
        Path(f"{workspace.search_index_sqlite}{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    )

    with sqlite3.connect(workspace.search_index_sqlite) as connection:
        table_rows = {row[1]: row for row in connection.execute("PRAGMA table_list")}
        assert table_rows["documents"][5] == 1
        assert table_rows["index_metadata"][5] == 1
        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'documents_fts'"
        ).fetchone()[0]
        assert "content = 'documents'" in fts_sql
        assert "tokenize = 'trigram'" in fts_sql
        assert connection.execute(
            "SELECT rowid, kind, record_id FROM documents ORDER BY rowid"
        ).fetchall() == [
            (1, "transcript", "T000001"),
            (1_000_000_001, "evidence", "E000001"),
        ]


def test_zero_evidence_ledger_rebuilds_and_searches_transcript_only(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(tmp_path, "demo-search")
    transcript_manifest, segments, evidence_manifest, evidence_records = _bundle(evidence_texts=())

    assert evidence_records == ()
    assert evidence_manifest.evidence_count == 0
    assert evidence_manifest.evidence_artifact.size_bytes == 0
    assert isinstance(evidence_manifest.evidence_artifact, EvidenceLedgerArtifactRef)
    verified = verify_index_rebuildable(
        "demo-search", transcript_manifest, segments, evidence_manifest, evidence_records
    )
    built = build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )
    result = search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        segments[0].normalized_text[:3],
        10,
    )

    assert verified.evidence_count == 0
    assert built.metadata.semantic_fingerprint == verified.semantic_fingerprint
    assert [item.record_id for item in result.hits] == ["T000001"]


def test_japanese_match_and_bounded_short_literal_search(tmp_path: Path) -> None:
    workspace, transcript_manifest, segments, evidence_manifest, evidence_records = _build(tmp_path)

    match = search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        "日本語",
        10,
    )
    assert match.mode is SearchQueryMode.FTS5_LITERAL_MATCH
    assert match.scan_limit_per_kind is None
    assert [item.record_id for item in match.hits] == ["T000001", "E000001"]

    two_char = search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        "日本",
        10,
    )
    assert two_char.mode is SearchQueryMode.BOUNDED_LITERAL_SCAN
    assert two_char.scan_limit_per_kind == SHORT_QUERY_SCAN_LIMIT_PER_KIND
    assert [item.record_id for item in two_char.hits] == ["T000001", "E000001"]

    one_char = search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        "語",
        1,
    )
    assert one_char.mode is SearchQueryMode.BOUNDED_LITERAL_SCAN
    assert len(one_char.hits) == 1
    assert one_char.truncated is True


def test_match_query_is_literal_and_sql_injection_safe(tmp_path: Path) -> None:
    workspace, transcript_manifest, segments, evidence_manifest, evidence_records = _build(tmp_path)

    injected = search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        '日本語" OR 1=1 --',
        100,
    )
    assert injected.mode is SearchQueryMode.FTS5_LITERAL_MATCH
    assert injected.hits == []

    dropped = search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        "日本語'); DROP TABLE documents; --",
        100,
    )
    assert dropped.hits == []
    assert search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        "日本語",
        10,
    ).hits


@pytest.mark.parametrize(
    ("query", "limit"),
    [
        ("", 10),
        ("   ", 10),
        ("x" * 257, 10),
        ("bad\x00query", 10),
        ("valid", 0),
        ("valid", 101),
        ("valid", True),
    ],
)
def test_query_and_limit_are_bounded_before_opening_the_index(
    tmp_path: Path, query: str, limit: int
) -> None:
    workspace = JobWorkspace.create(tmp_path, "demo-search")
    transcript_manifest, segments, evidence_manifest, evidence_records = _bundle()
    with pytest.raises(VepError) as captured:
        search_index(
            workspace,
            "demo-search",
            transcript_manifest,
            segments,
            evidence_manifest,
            evidence_records,
            query,
            limit,
        )
    assert captured.value.code == "SEARCH_QUERY_INVALID"
    if query:
        assert query not in repr(captured.value)


def test_semantic_staleness_and_rebuild(tmp_path: Path) -> None:
    workspace, old_transcript, old_segments, old_evidence, old_records = _build(tmp_path)
    new_transcript, new_segments, new_evidence, new_records = _bundle(
        transcript_texts=("更新後の日本語テキストです",),
        evidence_texts=("更新後の観察記録です",),
    )

    with pytest.raises(VepError) as captured:
        search_index(
            workspace,
            "demo-search",
            new_transcript,
            new_segments,
            new_evidence,
            new_records,
            "更新後",
            10,
        )
    assert captured.value.code == "INDEX_STALE"

    rebuilt = build_index(
        workspace,
        "demo-search",
        new_transcript,
        new_segments,
        new_evidence,
        new_records,
    )
    assert rebuilt.reused is False
    assert search_index(
        workspace,
        "demo-search",
        new_transcript,
        new_segments,
        new_evidence,
        new_records,
        "更新後",
        10,
    ).hits
    with pytest.raises(VepError) as old_context:
        search_index(
            workspace,
            "demo-search",
            old_transcript,
            old_segments,
            old_evidence,
            old_records,
            "日本語",
            10,
        )
    assert old_context.value.code == "INDEX_STALE"


def test_document_tamper_is_rejected_and_cache_can_be_rebuilt(tmp_path: Path) -> None:
    workspace, transcript_manifest, segments, evidence_manifest, evidence_records = _build(tmp_path)
    with closing(sqlite3.connect(workspace.search_index_sqlite)) as connection, connection:
        connection.execute(
            "UPDATE documents SET content = ? WHERE record_id = ?",
            ("改ざんされた内容", "T000001"),
        )

    with pytest.raises(VepError) as captured:
        search_index(
            workspace,
            "demo-search",
            transcript_manifest,
            segments,
            evidence_manifest,
            evidence_records,
            "日本語",
            10,
        )
    assert captured.value.code == "INDEX_INVALID"

    rebuilt = build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )
    assert rebuilt.reused is False
    assert search_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
        "日本語",
        10,
    ).hits


def test_coherent_document_metadata_tamper_is_rejected_and_rebuilt(tmp_path: Path) -> None:
    workspace, transcript_manifest, segments, evidence_manifest, evidence_records = _build(tmp_path)
    changed = "改変された正規化テキスト"
    with closing(sqlite3.connect(workspace.search_index_sqlite)) as connection, connection:
        connection.execute(
            "UPDATE documents SET content = ?, content_sha256 = ? WHERE rowid = ?",
            (changed, sha256_bytes(changed.encode("utf-8")), 1),
        )
        rows = connection.execute(
            """
            SELECT rowid, kind, record_id, start_ms, end_ms, content, content_sha256
            FROM documents ORDER BY rowid
            """
        ).fetchall()
        documents = tuple(search_module._document_from_row(row) for row in rows)
        stored = connection.execute(
            "SELECT metadata_json FROM index_metadata WHERE singleton = 1"
        ).fetchone()[0]
        metadata = json.loads(stored)
        metadata["documents_fingerprint"] = search_module._documents_fingerprint(documents)
        metadata["indexed_utf8_bytes"] = sum(
            len(item.content.encode("utf-8")) for item in documents
        )
        connection.execute(
            "UPDATE index_metadata SET metadata_json = ? WHERE singleton = 1",
            (canonical_json_bytes(metadata).decode("utf-8"),),
        )

    with pytest.raises(VepError) as captured:
        search_index(
            workspace,
            "demo-search",
            transcript_manifest,
            segments,
            evidence_manifest,
            evidence_records,
            "日本語",
            10,
        )
    assert captured.value.code == "INDEX_INVALID"

    rebuilt = build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )
    assert rebuilt.reused is False


def test_reuses_only_a_fully_verified_current_index(tmp_path: Path) -> None:
    workspace, transcript_manifest, segments, evidence_manifest, evidence_records = _build(tmp_path)
    reused = build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )
    assert reused.reused is True


def test_search_fails_closed_while_cache_lock_is_held(tmp_path: Path) -> None:
    workspace, transcript_manifest, segments, evidence_manifest, evidence_records = _build(tmp_path)
    with (
        exclusive_file_lock(
            workspace.search_lock_path,
            busy_code="holder-busy",
            busy_message="holder busy",
            failure_code="holder-failed",
            failure_message="holder failed",
        ),
        pytest.raises(VepError) as captured,
    ):
        search_index(
            workspace,
            "demo-search",
            transcript_manifest,
            segments,
            evidence_manifest,
            evidence_records,
            "日本語",
            10,
        )
    assert captured.value.code == "SEARCH_INDEX_BUSY"


def test_rebuildability_rejects_artifact_hash_mismatch() -> None:
    transcript_manifest, segments, evidence_manifest, evidence_records = _bundle()
    bad_artifact = transcript_manifest.segments_artifact.model_copy(
        update={"content_sha256": "0" * 64}
    )
    bad_manifest = transcript_manifest.model_copy(update={"segments_artifact": bad_artifact})

    with pytest.raises(VepError) as captured:
        verify_index_rebuildable(
            "demo-search", bad_manifest, segments, evidence_manifest, evidence_records
        )
    assert captured.value.code == "INDEX_INVALID"


def test_rebuildability_rejects_indexed_text_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    transcript_manifest, segments, evidence_manifest, evidence_records = _bundle()
    monkeypatch.setattr(search_module, "MAX_INDEXED_UTF8_BYTES", 1)
    with pytest.raises(VepError) as captured:
        verify_index_rebuildable(
            "demo-search", transcript_manifest, segments, evidence_manifest, evidence_records
        )
    assert captured.value.code == "INDEX_INPUT_TOO_LARGE"


def test_fts_shadow_tamper_is_rejected_and_rebuilt(tmp_path: Path) -> None:
    workspace, transcript_manifest, segments, evidence_manifest, evidence_records = _build(tmp_path)
    with closing(sqlite3.connect(workspace.search_index_sqlite)) as connection, connection:
        indexed_content = connection.execute(
            "SELECT content FROM documents WHERE rowid = ?", (1,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO documents_fts(documents_fts, rowid, content)
            VALUES ('delete', ?, ?)
            """,
            (1, indexed_content),
        )

    with pytest.raises(VepError) as captured:
        search_index(
            workspace,
            "demo-search",
            transcript_manifest,
            segments,
            evidence_manifest,
            evidence_records,
            segments[0].normalized_text[:3],
            10,
        )
    assert captured.value.code == "INDEX_INVALID"

    rebuilt = build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )
    assert rebuilt.reused is False


def test_build_cleans_only_expected_crash_staging_residue(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(tmp_path, "demo-search")
    transcript_manifest, segments, evidence_manifest, evidence_records = _bundle()
    residue = workspace.safe_path("search/staging/" + "a" * 32 + ".sqlite3")
    residue.write_bytes(b"incomplete")
    residue_sidecar = Path(f"{residue}-journal")
    residue_sidecar.write_bytes(b"journal")

    built = build_index(
        workspace,
        "demo-search",
        transcript_manifest,
        segments,
        evidence_manifest,
        evidence_records,
    )

    assert built.reused is False
    assert not residue.exists()
    assert not residue_sidecar.exists()

    invalid = workspace.safe_path("search/staging/unexpected.txt")
    invalid.write_bytes(b"do not delete")
    with pytest.raises(VepError) as captured:
        build_index(
            workspace,
            "demo-search",
            transcript_manifest,
            segments,
            evidence_manifest,
            evidence_records,
        )
    assert captured.value.code == "INDEX_INVALID"
    assert invalid.read_bytes() == b"do not delete"


def test_database_contains_only_searchable_text_and_canonical_metadata(tmp_path: Path) -> None:
    workspace, _, _, _, _ = _build(tmp_path)
    database_bytes = workspace.search_index_sqlite.read_bytes()
    assert b"raw-only-marker" not in database_bytes
    assert b"evidence-private-flag" not in database_bytes

    with sqlite3.connect(workspace.search_index_sqlite) as connection:
        contents = connection.execute(
            "SELECT kind, content FROM documents ORDER BY rowid"
        ).fetchall()
    assert contents == [
        ("transcript", "これは 日本語の原文です"),
        ("evidence", "日本語の説明が映像内で明示された。"),
    ]
