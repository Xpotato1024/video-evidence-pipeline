from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MANIFEST_PATH = Path(__file__).parents[1] / "tools" / "ffmpeg-toolchain.json"
INSTALLER_PATH = Path(__file__).parents[1] / "scripts" / "install_ffmpeg.ps1"


def test_ffmpeg_toolchain_manifest_is_exactly_pinned() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    version = manifest["version"]

    assert manifest["schema_version"] == "1.0"
    assert manifest["platform"] == "windows-amd64"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert manifest["archive"]["url"].endswith(f"/{version}/{manifest['archive']['name']}")
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["archive"]["sha256"])

    for tool_name in ("ffmpeg", "ffprobe"):
        tool = manifest["executables"][tool_name]
        assert tool["version_prefix"].startswith(f"{tool_name} version {version}")
        assert re.fullmatch(r"[0-9a-f]{64}", tool["sha256"])
        assert tool["filename"] == f"{tool_name}.exe"


def test_ffmpeg_installer_uses_the_manifest_and_no_moving_latest_alias() -> None:
    installer = INSTALLER_PATH.read_text(encoding="utf-8")

    assert "tools\\ffmpeg-toolchain.json" in installer
    assert "latest" not in installer.lower()
    assert "System.Security.Cryptography.SHA256" in installer
    assert "Expand-Archive" in installer
    assert "AddToUserPath" in installer
    assert "VEP_FFMPEG_BIN_DIR" in installer
    assert "StartsWith($tool.version_prefix" in installer
    assert "Remove-Item -LiteralPath $temporaryRoot -Recurse -Force" in installer


def _installer_result(
    *args: str,
    local_app_data: Path,
    installer_path: Path = INSTALLER_PATH,
    temp_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    if temp_root is not None:
        environment["TEMP"] = str(temp_root)
        environment["TMP"] = str(temp_root)
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer_path),
            *args,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
        env=environment,
    )


def _install_root(local_app_data: Path) -> Path:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    relative = manifest["install"]["windows_user_local_relative_root"].replace("/", os.sep)
    return local_app_data / relative


@pytest.mark.parametrize(
    ("options", "expected_changes"),
    [
        ((), []),
        (("-SetUserEnvironment",), ["saved-selection"]),
        (("-AddToUserPath",), ["saved-selection", "saved-path"]),
    ],
)
def test_installer_persistence_requires_explicit_opt_in(tmp_path, options, expected_changes):
    """Exercise the install completion flow without ever writing User settings."""
    if os.name != "nt" or shutil.which("powershell.exe") is None:
        pytest.skip("Windows PowerShell is required")
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    completion = "$temporaryRoot = $null" + installer.split("$temporaryRoot = $null", 1)[1]
    root = str(tmp_path).replace("'", "''")
    harness = tmp_path / "completion.ps1"
    harness.write_text(
        "param([switch]$VerifyOnly, [switch]$AddToUserPath, [switch]$SetUserEnvironment)\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"$installRoot = '{root}'\n$binDir = '{root}'\n"
        "function Assert-InstalledToolchain {}\n"
        "function Ensure-UserToolchainEnvironment { Write-Output 'saved-selection' }\n"
        "function Ensure-UserPath { Write-Output 'saved-path' }\n" + completion,
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(harness), *options],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    changes = [line for line in result.stdout.splitlines() if line.startswith("saved-")]
    assert changes == expected_changes
    if not options:
        assert "$env:VEP_FFMPEG_BIN_DIR = '" in result.stdout
        assert "User environment unchanged" in result.stdout


WINDOWS_POWERSHELL = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is required for installer smoke tests",
)


@WINDOWS_POWERSHELL
def test_installer_verify_only_valid_install_passes() -> None:
    local_app_data_value = os.environ.get("LOCALAPPDATA")
    if not local_app_data_value:
        pytest.skip("LOCALAPPDATA is not configured")
    bin_dir = _install_root(Path(local_app_data_value)) / "bin"
    if not all((bin_dir / f"{name}.exe").is_file() for name in ("ffmpeg", "ffprobe")):
        pytest.skip("pinned local FFmpeg installation is not present")

    result = _installer_result("-VerifyOnly", local_app_data=Path(local_app_data_value))

    assert result.returncode == 0, result.stdout + result.stderr


@WINDOWS_POWERSHELL
def test_installer_verify_only_missing_install_fails_without_creation(tmp_path: Path) -> None:
    local_app_data = tmp_path / "missing-localappdata"

    result = _installer_result("-VerifyOnly", local_app_data=local_app_data)

    assert result.returncode != 0
    assert not local_app_data.exists()


@WINDOWS_POWERSHELL
def test_installer_verify_only_partial_install_fails_closed(tmp_path: Path) -> None:
    local_app_data = tmp_path / "partial-localappdata"
    install_root = _install_root(local_app_data)
    (install_root / "bin").mkdir(parents=True)
    (install_root / "bin" / "ffmpeg.exe").write_bytes(b"corrupt")

    result = _installer_result("-VerifyOnly", local_app_data=local_app_data)

    assert result.returncode != 0
    assert install_root.exists()


@WINDOWS_POWERSHELL
def test_installer_archive_hash_mismatch_fails_and_does_not_install(tmp_path: Path) -> None:
    local_app_data = tmp_path / "archive-mismatch-localappdata"
    bad_archive = tmp_path / "bad.zip"
    temp_root = tmp_path / "installer-temp"
    bad_archive.write_bytes(b"not the pinned archive")

    result = _installer_result(
        "-ArchivePath",
        str(bad_archive),
        local_app_data=local_app_data,
        temp_root=temp_root,
    )

    assert result.returncode != 0
    assert not _install_root(local_app_data).exists()
    assert not list((temp_root / "codex" / "video-evidence-pipeline").glob("ffmpeg-install-*"))


@WINDOWS_POWERSHELL
def test_installer_rejects_version_mismatch_with_pinned_hashes(tmp_path: Path) -> None:
    local_app_data_value = os.environ.get("LOCALAPPDATA")
    if not local_app_data_value:
        pytest.skip("LOCALAPPDATA is not configured")
    source_bin = _install_root(Path(local_app_data_value)) / "bin"
    if not all((source_bin / f"{name}.exe").is_file() for name in ("ffmpeg", "ffprobe")):
        pytest.skip("pinned local FFmpeg installation is not present")

    installer_root = tmp_path / "installer-copy"
    manifest_root = tmp_path / "tools"
    installer_root.mkdir()
    manifest_root.mkdir()
    shutil.copy2(INSTALLER_PATH, installer_root / INSTALLER_PATH.name)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["executables"]["ffmpeg"]["version_prefix"] = "ffmpeg version 0.0.0"
    (manifest_root / MANIFEST_PATH.name).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    local_app_data = tmp_path / "version-mismatch-localappdata"
    target_bin = _install_root(local_app_data) / "bin"
    target_bin.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy2(source_bin / f"{name}.exe", target_bin / f"{name}.exe")

    result = _installer_result(
        "-VerifyOnly",
        local_app_data=local_app_data,
        installer_path=installer_root / INSTALLER_PATH.name,
    )

    assert result.returncode != 0


@WINDOWS_POWERSHELL
def test_installer_rejects_reparse_point_bin_dir(tmp_path: Path) -> None:
    local_app_data = tmp_path / "junction-localappdata"
    install_root = _install_root(local_app_data)
    target_bin = tmp_path / "target-bin"
    target_bin.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        (target_bin / f"{name}.exe").write_bytes(b"target")
    bin_dir = install_root / "bin"
    install_root.mkdir(parents=True)
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(bin_dir), str(target_bin)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0 or not getattr(bin_dir, "is_junction", lambda: False)():
        pytest.skip("junction creation is unavailable")

    try:
        verification = _installer_result("-VerifyOnly", local_app_data=local_app_data)
        assert verification.returncode != 0
    finally:
        bin_dir.rmdir()


@WINDOWS_POWERSHELL
def test_installer_rejects_linked_executable(tmp_path: Path) -> None:
    local_app_data = tmp_path / "linked-localappdata"
    install_root = _install_root(local_app_data)
    bin_dir = install_root / "bin"
    bin_dir.mkdir(parents=True)
    target = tmp_path / "target.exe"
    target.write_bytes(b"target")
    try:
        (bin_dir / "ffmpeg.exe").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = _installer_result("-VerifyOnly", local_app_data=local_app_data)

    assert result.returncode != 0
