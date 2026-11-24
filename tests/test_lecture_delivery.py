from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_delivery_pipeline import _approve, _human_review_ready
from typer.testing import CliRunner

from video_evidence_pipeline import application
from video_evidence_pipeline.cli import app
from video_evidence_pipeline.delivery import lecture
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.errors import VepError


def _write(path: Path, payload: object) -> Path:
    path.write_bytes(pretty_json_bytes(payload))
    return path


def _options(path: Path) -> Path:
    return _write(
        path,
        {
            "schema_version": "2.0",
            "version": "v002",
            "output_mode": "worked_explanation",
            "profile": {
                "profile_id": "example",
                "version": "1",
                "language": "en",
                "rules": [{"scope": "calculation", "text": "Retain intermediate results."}],
            },
        },
    )


def _context(role: str, context: str) -> dict[str, object]:
    return {
        "role": role,
        "provider": "fixture",
        "model": "lecture-fixture",
        "version": "1",
        "executor": "pytest",
        "context_id": context,
        "execution_mode": "test_fixture",
        "external_api_used": False,
    }


def _fixture_content(request) -> dict[str, object]:
    ref = request.sources[0].id
    coverage = {
        "concepts": ["introduction"],
        "definitions": ["introduction"],
        "derivations": ["definition"],
        "conditions": ["definition"],
        "worked_examples": ["example"],
    }
    return {
        "schema_version": "2.0",
        "request_fingerprint": request.request_fingerprint,
        "author": _context("author", "author-context"),
        "title": "Lecture example",
        "language": request.profile.language,
        "output_mode": request.output_mode,
        "sources": [source.model_dump(mode="json") for source in request.sources],
        "blocks": [
            {
                "id": "introduction",
                "type": "paragraph",
                "text": "Mean of two values",
                "source_refs": [ref],
            },
            {"id": "definition", "type": "equation", "equation_id": "mean", "source_refs": [ref]},
            {
                "id": "example",
                "type": "steps",
                "source_refs": [ref],
                "steps": [
                    {
                        "id": "substitute",
                        "title": "Compute the mean",
                        "rationale": "Add the values and divide by their count.",
                        "symbolic": r"m=\frac{a+b}{2}",
                        "substitution": r"m=\frac{2+4}{2}",
                        "intermediate": r"m=\frac{6}{2}",
                        "result": "m=3",
                        "source_refs": [ref],
                    }
                ],
            },
        ],
        "equations": [
            {
                "id": "mean",
                "tex": r"m=\frac{a+b}{2}",
                "symbol_table": {"m": "arithmetic mean", "a": "first value", "b": "second value"},
                "assumptions": ["Two observations"],
                "source_refs": [ref],
            }
        ],
        "figures": [],
        "coverage": [
            {
                "topic": topic,
                "status": "covered" if topic in coverage else "not_applicable",
                "block_ids": coverage.get(topic, []),
                "note": None if topic in coverage else "Synthetic source has no relevant material.",
            }
            for topic in sorted(lecture._TOPICS)
        ],
    }


def _fake_render(content: Path, stage: Path, *, assets: Path | None) -> None:
    assert assets is None
    (stage / "sites").mkdir()
    files = {}
    for name in lecture._RENDER_FILES - {"render-receipt.json"}:
        payload = b"%PDF-fixture" if name.endswith(".pdf") else b"fixture"
        (stage / name).write_bytes(payload)
        files[name] = {"sha256": sha256_bytes(payload), "size_bytes": len(payload)}
    _write(
        stage / "render-receipt.json",
        {
            "schema_version": "2.0",
            "content_sha256": sha256_file(content),
            "files": files,
            "validation": {
                "external_network": False,
                "javascript_in_viewer": False,
                "approval": "not_evaluated",
            },
        },
    )


def _audit(rendered: Path, kind: str, context: str) -> dict[str, object]:
    frozen = lecture.prepare_lecture_audit(rendered, kind)
    assert "author" not in frozen["content"]
    assert "authoring-request.json" not in {item["path"] for item in frozen["review_files"]}
    return {
        "schema_version": "2.0",
        "kind": kind,
        "request_fingerprint": frozen["request_fingerprint"],
        "agent": _context("auditor", context),
        "checked_blocks": frozen["required_blocks"],
        "checked_figures": frozen["required_figures"],
        "checked_equations": frozen["required_equations"],
        "findings": [],
    }


def test_lecture_document_to_review_and_separate_approval(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "lecture.txt"
    source.write_text("Add 2 and 4 and divide by 2.", encoding="utf-8")
    documents = _write(
        tmp_path / "docs.json",
        {
            "job_id": "fixture-lecture",
            "sources": [{"id": "notes", "path": source.name, "sha256": sha256_file(source)}],
        },
    )
    options = _options(tmp_path / "input.json")
    prepared = tmp_path / "prepared"
    request, reused = lecture.prepare_lecture(documents, options, prepared, documents=True)
    assert not reused and request.sources[0].locator.sha256 == sha256_file(source)
    assert lecture.prepare_lecture(documents, options, prepared, documents=True) == (request, True)
    cli = CliRunner().invoke(
        app, ["lecture", "request-validate", str(prepared / "request.json"), "--json"]
    )
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output)["job_id"] == "fixture-lecture"

    content = _write(tmp_path / "content.json", _fixture_content(request))
    rendered = tmp_path / "render"
    monkeypatch.setattr(lecture, "_render", _fake_render)
    binding, reused = lecture.render_lecture(prepared / "request.json", content, rendered)
    assert not reused
    assert lecture.render_lecture(prepared / "request.json", content, rendered) == (binding, True)

    content_audit = _write(
        tmp_path / "content-audit.json", _audit(rendered, "content", "content-context")
    )
    visual_audit = _write(
        tmp_path / "visual-audit.json", _audit(rendered, "visual", "author-context")
    )
    bundle = tmp_path / "review-v002"
    with pytest.raises(VepError, match="fresh distinct contexts"):
        lecture.publish_lecture_review(rendered, content_audit, visual_audit, bundle)
    visual = _audit(rendered, "visual", "visual-context")
    visual["findings"] = [
        {
            "severity": "P1",
            "target_id": "mean",
            "detail": "Fixture finding",
            "source_refs": ["notes"],
        }
    ]
    _write(visual_audit, visual)
    with pytest.raises(VepError, match="P0/P1"):
        lecture.publish_lecture_review(rendered, content_audit, visual_audit, bundle)
    visual["findings"] = []
    _write(visual_audit, visual)
    manifest, reused = lecture.publish_lecture_review(rendered, content_audit, visual_audit, bundle)
    assert not reused
    assert lecture.validate_lecture_bundle(bundle) == manifest
    assert lecture.publish_lecture_review(rendered, content_audit, visual_audit, bundle) == (
        manifest,
        True,
    )
    assert manifest.state == "HUMAN_REVIEW_READY"
    before = (bundle / "bundle-manifest.json").read_bytes()
    approval = {
        "target_sha256": manifest.approval_target_sha256,
        "reviewer": "Fixture reviewer",
        "basis": "Fixture human assertion",
    }
    with pytest.raises(VepError, match="human confirmation"):
        lecture.approve_lecture_bundle(bundle, **approval, confirm=False)
    record, path, reused = lecture.approve_lecture_bundle(bundle, **approval, confirm=True)
    assert not reused and path == tmp_path / "review-v002.approval.json"
    assert lecture.validate_lecture_approval(bundle) == (record, path)
    assert lecture.approve_lecture_bundle(bundle, **approval, confirm=True) == (record, path, True)
    assert before == (bundle / "bundle-manifest.json").read_bytes()
    (bundle / "review.pdf").write_bytes(b"%PDF-different")
    with pytest.raises(VepError, match="inventory"):
        lecture.validate_lecture_approval(bundle)
    source.write_text("Changed", encoding="utf-8")
    with pytest.raises(VepError, match="SHA-256 changed"):
        lecture.prepare_lecture(documents, options, tmp_path / "new", documents=True)


def test_audit_rules_and_omissions_use_frozen_sources(tmp_path: Path, monkeypatch) -> None:
    sources = []
    for name in ("handout", "board"):
        path = tmp_path / f"{name}.txt"
        path.write_text(f"Verified {name}", encoding="utf-8")
        sources.append({"id": name, "path": path.name, "sha256": sha256_file(path)})
    index = _write(tmp_path / "docs.json", {"job_id": "fixture-review", "sources": sources})
    options = _options(tmp_path / "options.json")
    values = json.loads(options.read_text(encoding="utf-8"))
    content_rule = "Trace the operation from the board."
    visual_rule = "Find the illustrated boundary."
    values["profile"]["rules"].extend(
        [
            {"scope": "content_audit", "text": content_rule},
            {"scope": "visual_audit", "text": visual_rule},
        ]
    )
    _write(options, values)
    prepared = tmp_path / "prepared"
    request, _ = lecture.prepare_lecture(index, options, prepared, documents=True)
    draft = _fixture_content(request)
    omitted = draft["sources"].pop()["id"]
    content = _write(tmp_path / "content.json", draft)
    rendered = tmp_path / "render"
    monkeypatch.setattr(lecture, "_render", _fake_render)
    lecture.render_lecture(prepared / "request.json", content, rendered)
    for kind, included, excluded in (
        ("content", content_rule, visual_rule),
        ("visual", visual_rule, content_rule),
    ):
        criteria = lecture.prepare_lecture_audit(rendered, kind)["criteria"]
        assert included in criteria and excluded not in criteria

    audit = _audit(rendered, "content", "reader")
    finding = {
        "severity": "P1",
        "target_id": "derivations",
        "detail": "A required operation is missing.",
        "source_refs": [omitted],
    }
    audit["findings"] = [finding]
    content_audit = _write(tmp_path / "content-audit.json", audit)
    visual_audit = _write(tmp_path / "visual-audit.json", _audit(rendered, "visual", "viewer"))
    with pytest.raises(VepError, match="P0/P1"):
        lecture.publish_lecture_review(rendered, content_audit, visual_audit, tmp_path / "review")
    finding["source_refs"] = ["unverified"]
    _write(content_audit, audit)
    with pytest.raises(VepError, match="unknown evidence"):
        lecture.publish_lecture_review(rendered, content_audit, visual_audit, tmp_path / "review")


def test_lecture_regeneration_freezes_verified_workspace_and_archive(tmp_path: Path) -> None:
    workspace, _draft, audit = _human_review_ready(tmp_path, "lecture-legacy")
    target = audit.human_review.approval_target_sha256
    _approve(workspace, target)
    transitions = workspace.transitions_jsonl.read_bytes()
    archive_dir = tmp_path / "archive"
    archive = application.export_study_bundle_job(
        workspace.root, archive_dir, confirm_private_export=True
    ).manifest
    archive_before = (archive_dir / "bundle-manifest.json").read_bytes()
    options = _options(tmp_path / "input.json")
    from_workspace, _ = lecture.prepare_lecture(
        workspace.root, options, tmp_path / "from-workspace"
    )
    from_archive, _ = lecture.prepare_lecture(
        archive_dir, options, tmp_path / "from-archive", archive=True
    )
    assert from_archive.source_state == "APPROVED_ARCHIVE"
    assert from_archive.legacy_draft_sha256 == archive.approved_draft_sha256
    assert from_archive.evidence_bundle_fingerprint == from_workspace.evidence_bundle_fingerprint
    assert from_archive.sources == from_workspace.sources
    assert from_archive.requirements == from_workspace.requirements
    assert from_archive.ambiguities == from_workspace.ambiguities
    assert not from_archive.external_api_allowed
    assert transitions == workspace.transitions_jsonl.read_bytes()
    assert archive_before == (archive_dir / "bundle-manifest.json").read_bytes()


def test_portable_mdx_edits_only_update_typed_fields() -> None:
    from copy import deepcopy

    from video_evidence_pipeline.delivery.lecture_source import apply_mdx_edits

    content = {
        "title": "Worked example",
        "blocks": [
            {"id": "statement", "type": "heading", "text": "Set-up", "level": 2},
            {"id": "given", "type": "paragraph", "text": "Use a & b."},
            {"id": "formula", "type": "equation", "equation_id": "mean"},
            {
                "id": "example",
                "type": "steps",
                "steps": [
                    {
                        "id": "calculate",
                        "title": "Calculate",
                        "rationale": "Count the terms.",
                        "symbolic": "a+b",
                        "substitution": None,
                        "intermediate": None,
                        "result": "2",
                    }
                ],
            },
        ],
        "equations": [{"id": "mean", "tex": "a+b"}],
        "figures": [],
    }
    original = (
        "# Worked example\n\n{/* vep:block statement */}\n\n## Set-up\n\n"
        "{/* vep:block given */}\n\nUse a &amp; b.\n\nSource: reference\n\n"
        '{/* vep:block formula */}\n\n```math id="mean"\na+b\n```\n\n'
        '{/* vep:block example */}\n\n<Steps>\n\n<Step title="Calculate">\n\n'
        'Count the terms.\n\n```math id="example-calculate-symbolic"\na+b\n```\n\n'
        '```math id="example-calculate-result"\n2\n```\n\n</Step>\n\n</Steps>\n\n'
        "{/* vep:sources */}\n\n## Sources\n\n- reference\n"
    )
    edited = (
        original.replace("Use a &amp; b.", "Use two values.")
        .replace('math id="mean"\na+b', 'math id="mean"\n(a+b)/2')
        .replace("Count the terms.", "Count both values.")
        .replace('math id="example-calculate-result"\n2', 'math id="example-calculate-result"\n3')
    )
    result = apply_mdx_edits(deepcopy(content), original, edited)
    assert result["blocks"][1]["text"] == "Use two values."
    assert result["equations"][0]["tex"] == "(a+b)/2"
    assert result["blocks"][3]["steps"][0]["rationale"] == "Count both values."
    assert result["blocks"][3]["steps"][0]["result"] == "3"
    with pytest.raises(VepError, match="source reference"):
        apply_mdx_edits(
            deepcopy(content), original, edited.replace("Source: reference", "Source: invented")
        )
    with pytest.raises(VepError, match="unsupported markup"):
        apply_mdx_edits(deepcopy(content), original, edited.replace("Use two values.", "{run()}"))


def test_portable_list_and_operation_edits_keep_their_structure() -> None:
    from video_evidence_pipeline.delivery.lecture_source import apply_mdx_edits

    content = {
        "title": "Guide",
        "blocks": [
            {"id": "axes", "type": "bullets", "title": "Axes", "items": ["Right", "Up"]},
            {
                "id": "trace",
                "type": "steps",
                "steps": [
                    {
                        "id": "level",
                        "type": "operation",
                        "title": "Pick",
                        "rationale": "Fix a level.",
                        "expression": "h=1",
                    },
                    {
                        "id": "join",
                        "type": "operation",
                        "title": "Join",
                        "rationale": "Connect points.",
                    },
                ],
            },
        ],
        "equations": [],
        "figures": [],
    }
    source = (
        "# Guide\n\n{/* vep:block axes */}\n\n**Axes**\n\n- Right\n- Up\n\n"
        '{/* vep:block trace */}\n\n<Steps>\n\n<Step title="Pick">\n\nFix a level.\n\n'
        '```math id="trace-level-expression"\nh=1\n```\n\n</Step>\n\n'
        '<Step title="Join">\n\nConnect points.\n\n</Step>\n\n</Steps>\n\n'
        "{/* vep:sources */}\n\n## Sources\n"
    )
    edited = (
        source.replace("Right", "Outward")
        .replace("h=1", "h=2")
        .replace("Connect points.", "Trace the curve.")
    )
    result = apply_mdx_edits(content, source, edited)["blocks"]
    assert result[0]["items"] == ["Outward", "Up"]
    assert result[1]["steps"][0]["expression"] == "h=2"
    assert result[1]["steps"][1]["rationale"] == "Trace the curve."


def test_portable_background_paragraphs_and_question_are_editable() -> None:
    from video_evidence_pipeline.delivery.lecture_source import apply_mdx_edits

    content = {
        "title": "Guide",
        "blocks": [
            {"id": "why", "type": "background", "title": "Why?", "paragraphs": ["One.", "Two."]}
        ],
        "figures": [],
        "equations": [],
    }
    source = '# Guide\n\n{/* vep:block why */}\n\n<Background title="Why?">\n\nOne.\n\nTwo.\n\nSource: a\n\n</Background>\n\n{/* vep:sources */}\n\n## Sources\n'
    edited = source.replace("Why?", "When?").replace("Two.", "Another reason.")
    block = apply_mdx_edits(content, source, edited)["blocks"][0]
    assert block["title"] == "When?"
    assert block["paragraphs"] == ["One.", "Another reason."]


def test_drawing_steps_require_distinct_figures_and_bound_reasons(tmp_path: Path) -> None:
    source = tmp_path / "lecture.txt"
    source.write_text("Plot the origin and the axis.", encoding="utf-8")
    docs = _write(
        tmp_path / "docs.json",
        {
            "job_id": "fixture-sketch",
            "sources": [{"id": "notes", "path": source.name, "sha256": sha256_file(source)}],
        },
    )
    request, _ = lecture.prepare_lecture(
        docs, _options(tmp_path / "opts.json"), tmp_path / "prep", documents=True
    )
    content = _fixture_content(request)
    ref = request.sources[0].id
    stages = [
        {
            "type": "operation",
            "id": key,
            "title": key,
            "rationale": "Draw it.",
            "explanation": "It fixes the construction.",
            "figure_id": f"fig-{key}",
            "source_refs": [ref],
        }
        for key in ("origin", "axis")
    ]
    content["blocks"][-1] = {
        "type": "procedure",
        "id": "example",
        "kind": "drawing",
        "steps": stages,
        "source_refs": [ref],
    }
    content["figures"] = [
        {
            "id": item["figure_id"],
            "kind": "geometry",
            "spec": {},
            "alt": item["id"],
            "caption": item["title"],
            "factuality": "derived_diagram",
            "source_refs": [ref],
        }
        for item in stages
    ]
    for item in content["coverage"]:
        if item["topic"] == "visuals":
            item.update(status="covered", block_ids=["example"])
    lecture._check_content(lecture.RichContent.model_validate(content), request)
    for key in (None, stages[0]["figure_id"], "absent"):
        stages[1]["figure_id"] = key
        with pytest.raises(ValueError, match="stage figure"):
            lecture.RichContent.model_validate(content)


def test_comparison_keeps_order_and_units_in_editable_cases() -> None:
    from pydantic import ValidationError

    from video_evidence_pipeline.delivery.lecture_source import apply_mdx_edits
    from video_evidence_pipeline.domain.lecture import ComparisonBlock

    cases = [{"label": "Angle", "items": ["Angle (rad)"]}, {"label": "Arc", "items": ["Arc (m)"]}]
    block = {
        "id": "pair",
        "type": "comparison",
        "source_refs": ["notes"],
        "title": "Compare",
        "cases": cases,
    }
    ComparisonBlock.model_validate(block)
    with pytest.raises(ValidationError):
        ComparisonBlock.model_validate({**block, "cases": cases[:1]})
    source = (
        "# Guide\n\n{/* vep:block pair */}\n\n**Compare**\n\n<Comparison>\n\n"
        '<ComparisonCase label="Angle">\n\n- Angle (rad)\n\n</ComparisonCase>\n\n'
        '<ComparisonCase label="Arc">\n\n- Arc (m)\n\n</ComparisonCase>\n\n'
        "</Comparison>\n\n{/* vep:sources */}\n\n## Sources\n"
    )
    content = {"title": "Guide", "blocks": [block], "equations": [], "figures": []}
    edited = (
        source.replace("Compare", "Circle")
        .replace('label="Arc"', 'label="Length"')
        .replace("Arc (m)", "Path (m)")
    )
    result = apply_mdx_edits(content, source, edited)["blocks"][0]
    assert result["title"] == "Circle"
    assert result["cases"] == [cases[0], {"label": "Length", "items": ["Path (m)"]}]
