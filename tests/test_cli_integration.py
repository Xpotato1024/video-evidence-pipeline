from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import yaml

from video_evidence_pipeline.domain.job import load_job_yaml, requires_transcript_refinement


def vep_executable() -> Path:
    name = "vep.exe" if os.name == "nt" else "vep"
    return Path(sys.executable).with_name(name)


def run_vep(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(vep_executable()), *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _workspace_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_cli_assignment_intent_and_job_guide_are_read_only(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.txt"
    source.write_text("synthetic transcript", encoding="utf-8")
    initialized = run_vep(
        tmp_path,
        "job",
        "init",
        "guided-summary",
        "--source-kind",
        "local_file",
        "--source",
        str(source),
        "--primary-input",
        "transcript",
        "--task-type",
        "summary",
        "--language",
        "ja",
        "--output-format",
        "markdown",
        "--purpose",
        "研修後に実行する重要事項を把握する",
        "--audience",
        "初めて参加する一般利用者",
        "--json",
    )
    assert initialized.returncode == 0, initialized.stderr
    workspace = tmp_path / ".local" / "jobs" / "guided-summary"
    job = load_job_yaml(workspace / "job.yaml")
    assert job.assignment.purpose == "研修後に実行する重要事項を把握する"
    assert job.assignment.audience == "初めて参加する一般利用者"

    before = _workspace_snapshot(workspace)
    json_result = run_vep(tmp_path, "job", "guide", str(workspace), "--json")
    human_result = run_vep(tmp_path, "job", "guide", str(workspace))
    after = _workspace_snapshot(workspace)

    assert json_result.returncode == 0, json_result.stderr
    payload = json.loads(json_result.stdout)
    assert payload["stored_state"] == "CREATED"
    assert payload["effective_state"] == "CREATED"
    assert payload["next_operation"] == "authorize"
    assert payload["guide_read_only"] is True
    assert payload["side_effects"]["workspace_write"] is True
    assert "next_operation: authorize" in human_result.stdout
    assert before == after
    assert not (workspace / "refinement/request.json").exists()
    assert not (workspace / "evidence/request.json").exists()


def test_cli_created_authorized_acquired_e2e(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.bin"
    content = b"small synthetic source"
    source.write_bytes(content)
    job_path = tmp_path / ".local" / "jobs" / "demo-report"

    initialized = run_vep(
        tmp_path,
        "job",
        "init",
        "demo-report",
        "--source-kind",
        "local_file",
        "--source",
        str(source),
        "--task-type",
        "report",
        "--language",
        "ja",
        "--output-format",
        "markdown",
        "--requirement",
        "cite two concrete observations",
        "--requirement",
        "separate facts from inference",
        "--json",
    )
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["state"] == "CREATED"
    job_data = yaml.safe_load((job_path / "job.yaml").read_text(encoding="utf-8"))
    expected_requirements = [
        "cite two concrete observations",
        "separate facts from inference",
    ]
    assert job_data["assignment"]["requirements"] == expected_requirements

    validated = run_vep(tmp_path, "job", "validate", str(job_path), "--json")
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["valid"] is True

    unauthorized = run_vep(
        tmp_path,
        "job",
        "authorize",
        str(job_path),
        "--basis",
        "authorized synthetic fixture",
        "--allow-local-processing",
        "--deny-external-api",
        "--json",
    )
    assert unauthorized.returncode == 3
    assert json.loads(unauthorized.stdout)["error"]["code"] == "AUTHORIZATION_CONFIRMATION_REQUIRED"
    assert "Traceback" not in unauthorized.stderr

    authorized = run_vep(
        tmp_path,
        "job",
        "authorize",
        str(job_path),
        "--confirm",
        "--basis",
        "authorized synthetic fixture",
        "--allow-local-processing",
        "--deny-external-api",
        "--confirmer",
        "test-user",
        "--json",
    )
    assert authorized.returncode == 0, authorized.stderr
    assert json.loads(authorized.stdout)["state"] == "AUTHORIZED"

    acquired = run_vep(tmp_path, "acquire", str(job_path), "--json")
    assert acquired.returncode == 0, acquired.stderr
    acquired_data = json.loads(acquired.stdout)
    assert acquired_data["state"] == "ACQUIRED"

    reacquired = run_vep(tmp_path, "acquire", str(job_path), "--json")
    assert reacquired.returncode == 0, reacquired.stderr
    assert json.loads(reacquired.stdout)["reused"] is True

    status = run_vep(tmp_path, "job", "status", str(job_path), "--json")
    assert status.returncode == 0, status.stderr
    status_data = json.loads(status.stdout)
    assert status_data["stored_state"] == "ACQUIRED"
    assert status_data["artifact_integrity"]["status"] == "valid"
    final_job_data = yaml.safe_load((job_path / "job.yaml").read_text(encoding="utf-8"))
    assert final_job_data["assignment"]["requirements"] == expected_requirements
    assert final_job_data["primary_input"]["kind"] == "video"
    assert requires_transcript_refinement(load_job_yaml(job_path / "job.yaml")) is True

    manifest = json.loads((job_path / "manifests/source-manifest.json").read_text(encoding="utf-8"))
    manifest_text = (job_path / "manifests/source-manifest.json").read_text(encoding="utf-8")
    artifact = manifest["artifact_record"]["artifact"]
    raw = job_path / artifact["relative_storage_path"]
    assert raw.read_bytes() == content
    assert artifact["content_sha256"] == acquired_data["content_sha256"]
    assert str(source.absolute()) not in manifest_text

    if os.name == "posix":
        private_directories = [
            tmp_path / ".local",
            tmp_path / ".local/jobs",
            job_path,
            job_path / "authorization",
            job_path / "source",
            job_path / "source/raw",
            raw.parent,
            job_path / "manifests",
            job_path / "manifests/stages",
            job_path / "logs",
        ]
        private_files = [
            job_path / "job.yaml",
            job_path / "authorization/authorization.json",
            job_path / "manifests/source-manifest.json",
            job_path / "manifests/stages/acquisition.json",
            job_path / "manifests/transitions.jsonl",
            raw,
        ]
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in private_directories)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_files)

    final_job_data["output"]["format"] = "html"
    (job_path / "job.yaml").write_text(
        yaml.safe_dump(final_job_data, sort_keys=False),
        encoding="utf-8",
    )
    stale = run_vep(tmp_path, "job", "validate", str(job_path), "--json")
    assert stale.returncode == 3
    assert json.loads(stale.stdout)["error"]["code"] == "AUTHORIZATION_STALE"


def test_cli_network_kind_is_denied_without_dedicated_permission(tmp_path: Path) -> None:
    locator = "https://example.invalid/video"
    initialized = run_vep(
        tmp_path,
        "job",
        "init",
        "network-demo",
        "--source-kind",
        "direct_http",
        "--source",
        locator,
        "--task-type",
        "report",
        "--language",
        "ja",
        "--output-format",
        "markdown",
        "--json",
    )
    assert initialized.returncode == 0
    job_path = tmp_path / ".local" / "jobs" / "network-demo"
    authorized = run_vep(
        tmp_path,
        "job",
        "authorize",
        str(job_path),
        "--confirm",
        "--basis",
        "authorized synthetic locator",
        "--allow-local-processing",
        "--deny-external-api",
        "--json",
    )
    assert authorized.returncode == 0
    acquired = run_vep(tmp_path, "acquire", str(job_path), "--json")
    assert acquired.returncode == 3
    payload = json.loads(acquired.stdout)
    assert payload["error"]["code"] == "NETWORK_ACQUISITION_NOT_AUTHORIZED"
    assert "Traceback" not in acquired.stderr


def test_cli_network_authorization_flag_is_persisted_separately(tmp_path: Path) -> None:
    initialized = run_vep(
        tmp_path,
        "job",
        "init",
        "network-authorized",
        "--source-kind",
        "direct_http",
        "--source",
        "https://example.invalid/video",
        "--task-type",
        "report",
        "--language",
        "ja",
        "--output-format",
        "markdown",
        "--json",
    )
    assert initialized.returncode == 0
    job_path = tmp_path / ".local" / "jobs" / "network-authorized"
    authorized = run_vep(
        tmp_path,
        "job",
        "authorize",
        str(job_path),
        "--confirm",
        "--basis",
        "authorized synthetic locator",
        "--allow-local-processing",
        "--allow-network-acquisition",
        "--deny-external-api",
        "--json",
    )
    payload = json.loads(authorized.stdout)
    assert authorized.returncode == 0
    assert payload["allow_network_acquisition"] is True
    assert payload["allow_external_api"] is False
    status = run_vep(tmp_path, "job", "status", str(job_path), "--json")
    authorization = json.loads(status.stdout)["authorization"]
    assert authorization["allow_network_acquisition"] is True
    assert authorization["allow_external_api"] is False


def test_cli_rejects_sensitive_query_without_echoing_it(tmp_path: Path) -> None:
    secret_value = "secret-query-value"
    locator = "https://example.invalid/video?" + "token=" + secret_value
    initialized = run_vep(
        tmp_path,
        "job",
        "init",
        "network-demo",
        "--source-kind",
        "direct_http",
        "--source",
        locator,
        "--task-type",
        "report",
        "--language",
        "ja",
        "--output-format",
        "markdown",
        "--json",
    )
    assert initialized.returncode == 2
    assert json.loads(initialized.stdout)["error"]["code"] == "SOURCE_LOCATOR_INVALID"
    assert secret_value not in initialized.stdout
    assert "Traceback" not in initialized.stderr


def test_cli_parser_failure_is_structured_json(tmp_path: Path) -> None:
    result = run_vep(tmp_path, "job", "validate", "--json")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_INVALID"
    assert "Traceback" not in result.stderr


def test_cli_runtime_config_failure_is_structured_without_secret_echo(tmp_path: Path) -> None:
    mistaken_secret = "synthetic-runtime-token.secret"
    result = run_vep(
        tmp_path,
        "acquire",
        "does-not-exist",
        "--box-token-env",
        mistaken_secret,
        "--json",
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_INVALID"
    assert mistaken_secret not in result.stdout
    assert mistaken_secret not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_invalid_enum_is_structured_json_without_locator_echo(tmp_path: Path) -> None:
    locator = "credential-like-locator-value"
    result = run_vep(
        tmp_path,
        "job",
        "init",
        "demo",
        "--source-kind",
        "unsupported",
        "--source",
        locator,
        "--task-type",
        "report",
        "--language",
        "ja",
        "--output-format",
        "markdown",
        "--json",
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_INVALID"
    assert locator not in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_invalid_cookie_source_does_not_echo_supplied_value(tmp_path: Path) -> None:
    mistaken_secret = "synthetic-cookie-secret"
    result = run_vep(
        tmp_path,
        "acquire",
        "does-not-exist",
        "--cookies-from-browser",
        mistaken_secret,
        "--json",
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_INVALID"
    assert mistaken_secret not in result.stdout
    assert mistaken_secret not in result.stderr
    assert "Traceback" not in result.stderr
