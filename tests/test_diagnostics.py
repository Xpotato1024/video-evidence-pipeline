from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from video_evidence_pipeline.cli import app
from video_evidence_pipeline.diagnostics import diagnose_installation
from video_evidence_pipeline.errors import configuration_error

ROOT = Path(__file__).parents[1]


def test_transcript_doctor_never_probes_ffmpeg_or_reads_job(tmp_path, monkeypatch):
    from video_evidence_pipeline.media import runner

    def forbidden(*args, **kwargs):
        raise AssertionError("transcript doctor must not probe media tools")

    monkeypatch.setattr(runner, "FFmpegRunner", forbidden)
    monkeypatch.setenv("VEP_FFMPEG_BIN_DIR", "secret-directory")
    report = diagnose_installation(ROOT)
    assert report["ok"], report
    assert report["mode"] == "transcript"
    assert len(report["lock_sha256"]) == 64
    assert "secret-directory" not in json.dumps(report)
    assert not tmp_path.joinpath(".local").exists()


def test_doctor_incomplete_wheel_only_root_has_remedies_without_writes(tmp_path):
    report = diagnose_installation(tmp_path)
    assert report["ok"] is False
    assert {check["code"] for check in report["checks"]} >= {
        "REPOSITORY_INCOMPLETE",
        "CHECKOUT_RUNTIME_MISMATCH",
        "SCHEMA_DRIFT",
    }
    assert list(tmp_path.iterdir()) == []
    assert all(check["remedy"] for check in report["checks"] if not check["ok"])


def test_media_doctor_reports_safe_error_and_nonzero_cli_exit(monkeypatch):
    from video_evidence_pipeline.media import runner

    class MissingTools:
        def discover_identities(self, **kwargs):
            raise configuration_error("MEDIA_TOOL_UNAVAILABLE", "sensitive tool output")

    monkeypatch.setattr(runner, "FFmpegRunner", MissingTools)
    result = CliRunner().invoke(
        app, ["doctor", "--repository-root", str(ROOT), "--media", "--json"]
    )
    assert result.exit_code == 2
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["checks"][-1]["code"] == "MEDIA_TOOL_UNAVAILABLE"
    assert "sensitive tool output" not in result.stdout


def test_doctor_success_json_cli():
    result = CliRunner().invoke(app, ["doctor", "--repository-root", str(ROOT), "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["ok"]
