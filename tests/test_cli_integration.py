from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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
        "--json",
    )
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["state"] == "CREATED"

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

    status = run_vep(tmp_path, "job", "status", str(job_path), "--json")
    assert status.returncode == 0, status.stderr
    status_data = json.loads(status.stdout)
    assert status_data["stored_state"] == "ACQUIRED"
    assert status_data["artifact_integrity"]["status"] == "valid"

    manifest = json.loads((job_path / "manifests/source-manifest.json").read_text(encoding="utf-8"))
    artifact = manifest["artifact_record"]["artifact"]
    raw = job_path / artifact["relative_storage_path"]
    assert raw.read_bytes() == content
    assert artifact["content_sha256"] == acquired_data["content_sha256"]


def test_cli_nonimplemented_kind_has_structured_safe_error(tmp_path: Path) -> None:
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
    assert acquired.returncode == 2
    payload = json.loads(acquired.stdout)
    assert payload["error"]["code"] == "SOURCE_KIND_NOT_IMPLEMENTED"
    assert "Traceback" not in acquired.stderr


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
    assert json.loads(initialized.stdout)["error"]["code"] == "CONFIG_INVALID"
    assert secret_value not in initialized.stdout
    assert "Traceback" not in initialized.stderr
