from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_delivery_pipeline import _approve, _human_review_ready
from typer.testing import CliRunner

from video_evidence_pipeline.application import export_job, export_study_bundle_job
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.delivery.study_bundle import validate_study_bundle
from video_evidence_pipeline.domain.study_bundle import portable_path
from video_evidence_pipeline.errors import VepError


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/a",
        "../a",
        "a/../b",
        "a//b",
        "C:/a",
        "a\\b",
        "a:b",
        "a/./b",
        "a./b",
        "a /b",
        "CON.txt",
        "a/NUL",
        "a/COM1",
        "a\x00b",
    ],
)
def test_portable_paths_reject_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        portable_path(value)


def test_study_export_requires_existing_human_approval_and_opt_in(tmp_path: Path) -> None:
    workspace, _draft, _audit = _human_review_ready(tmp_path, "study-gates")
    target = tmp_path / "archive"
    before = workspace.transitions_jsonl.read_bytes()
    with pytest.raises(VepError) as error:
        export_study_bundle_job(workspace.root, target)
    assert error.value.code == "STUDY_BUNDLE_CONFIRMATION_REQUIRED"
    with pytest.raises(VepError) as error:
        export_study_bundle_job(workspace.root, target, confirm_private_export=True)
    assert error.value.code == "STATE_GATE_FAILED"
    assert not target.exists()
    assert workspace.transitions_jsonl.read_bytes() == before


def test_portable_bundle_roundtrip_tampering_and_markdown_compatibility(tmp_path: Path) -> None:
    workspace, draft, audit = _human_review_ready(tmp_path, "study-roundtrip")
    _approve(workspace, audit.human_review.approval_target_sha256)
    target = tmp_path / "archive"
    before = workspace.transitions_jsonl.read_bytes()
    result = export_study_bundle_job(workspace.root, target, confirm_private_export=True)
    assert not result.reused
    assert workspace.transitions_jsonl.read_bytes() == before
    manifest = validate_study_bundle(target)
    assert manifest == result.manifest
    assert manifest.omitted_source_artifacts
    assert (target / "summary.md").read_bytes() == workspace.safe_path(
        draft.manifest.draft_artifact.relative_storage_path
    ).read_bytes()
    assert (target / ".gitattributes").read_bytes() == b"* -text\n"
    assert not (target / "source").exists()
    assert not (target / "authorization").exists()
    assert not (target / "job.yaml").exists()
    assert not (target / "logs").exists()
    replay = export_study_bundle_job(workspace.root, target, confirm_private_export=True)
    assert replay.reused and replay.manifest == manifest
    markdown = export_job(workspace.root)
    assert markdown.manifest.final_artifact.content_sha256 == manifest.approved_draft_sha256
    assert export_study_bundle_job(workspace.root, target, confirm_private_export=True).reused
    cli = CliRunner().invoke(app, ["bundle-validate", str(target), "--json"])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output)["file_count"] == len(manifest.files)
    empty = tmp_path / "caller-owned"
    empty.mkdir()
    with pytest.raises(VepError):
        export_study_bundle_job(workspace.root, empty, confirm_private_export=True)
    assert list(empty.iterdir()) == []
    with pytest.raises(VepError) as error:
        export_study_bundle_job(workspace.root, workspace.root / "bad", confirm_private_export=True)
    assert error.value.code == "STUDY_BUNDLE_DESTINATION_INVALID"
    for mode in ["change", "remove", "extra"]:
        damaged = tmp_path / f"damaged-{mode}"
        shutil.copytree(target, damaged)
        if mode == "change":
            data = (damaged / "summary.md").read_bytes()
            (damaged / "summary.md").write_bytes(bytes([data[0] ^ 1]) + data[1:])
        elif mode == "remove":
            (damaged / manifest.entrypoints["transcript_raw"]).unlink()
        else:
            (damaged / "unexpected.txt").write_text("not in inventory", encoding="utf-8")
        with pytest.raises(VepError):
            validate_study_bundle(damaged)
    cli_target = tmp_path / "cli-archive"
    cli = CliRunner().invoke(
        app,
        [
            "export",
            str(workspace.root),
            "--format",
            "study-bundle",
            "--destination",
            str(cli_target),
            "--confirm-private-export",
            "--json",
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output)["format"] == "study-bundle"
    assert validate_study_bundle(cli_target) == manifest
    workspace.root.rename(workspace.root.with_name("hidden-original-job"))
    moved = tmp_path / "moved-archive"
    target.rename(moved)
    assert not workspace.root.exists()
    assert validate_study_bundle(moved) == manifest
    repository = tmp_path / "git-source"
    shutil.copytree(moved, repository / "R001")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repository), *args], check=True, capture_output=True
        )

    git("init", "--quiet")
    git("-c", "core.autocrlf=true", "add", ".")
    git(
        "-c",
        "user.name=Test Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "Synthetic archive",
    )
    cloned = tmp_path / "git-clone"
    git("-c", "core.autocrlf=true", "clone", "--quiet", "--no-local", str(repository), str(cloned))
    assert validate_study_bundle(cloned / "R001") == manifest


def test_bundle_validator_rejects_directory_links(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is unavailable on this host")
    with pytest.raises(VepError) as error:
        validate_study_bundle(link)
    assert error.value.code == "STUDY_BUNDLE_PATH_INVALID"


def test_source_index_covers_raw_refined_and_supporting_evidence(tmp_path: Path) -> None:
    from video_evidence_pipeline.delivery.study_sources import build_source_index
    from video_evidence_pipeline.domain.hashing import sha256_bytes
    from video_evidence_pipeline.domain.serialization import fingerprint
    from video_evidence_pipeline.domain.study_bundle import StudyBundleFile
    from video_evidence_pipeline.domain.transcription import TranscriptSegment

    record = json.loads(
        (Path(__file__).parent / "fixtures/legacy-transcription/caption/segments.jsonl").read_text(
            encoding="utf-8"
        )
    )
    raw = TranscriptSegment.model_validate(record)
    refined = raw.model_copy(update={"normalized_text": "corrected technical term"})
    inventory = {}

    def add(relative: str, payload: bytes):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        inventory[relative] = StudyBundleFile(
            relative_path=relative, sha256=sha256_bytes(payload), size_bytes=len(payload)
        )

    raw_path = "transcript/segments/raw/transcript.jsonl"
    refined_path = "refinement/segments/refined/transcript.jsonl"
    add(raw_path, (raw.model_dump_json() + "\n").encode("utf-8"))
    add(refined_path, (refined.model_dump_json() + "\n").encode("utf-8"))
    asset = "supporting/text/notes.txt"
    add(asset, b"Original supporting note")
    refs = [
        {
            "kind": "transcript_segment",
            "ref_id": raw.segment_id,
            "start_ms": raw.start_ms,
            "end_ms": raw.end_ms,
            "content_sha256": fingerprint(raw),
        },
        {
            "kind": "transcript_segment",
            "ref_id": refined.segment_id,
            "start_ms": refined.start_ms,
            "end_ms": refined.end_ms,
            "content_sha256": fingerprint(refined),
        },
        {
            "kind": "notes",
            "ref_id": "S000001",
            "start_ms": None,
            "end_ms": None,
            "content_sha256": inventory[asset].sha256,
        },
    ]
    evidence = "evidence/records/example/evidence.jsonl"
    add(evidence, (json.dumps({"source_refs": refs}) + "\n").encode("utf-8"))
    index = build_source_index(tmp_path, inventory)
    assert {item.relative_path for item in index.bindings} == {raw_path, refined_path, asset}
    refs[0]["content_sha256"] = "f" * 64
    add(evidence, (json.dumps({"source_refs": refs}) + "\n").encode("utf-8"))
    with pytest.raises(VepError) as error:
        build_source_index(tmp_path, inventory)
    assert error.value.code == "STUDY_BUNDLE_SOURCE_REF_MISSING"
