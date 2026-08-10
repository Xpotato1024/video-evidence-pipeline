from __future__ import annotations

import json
import re
from pathlib import Path

MANIFEST_PATH = Path(__file__).parents[1] / "tools" / "ffmpeg-toolchain.json"


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
    installer = (Path(__file__).parents[1] / "scripts" / "install_ffmpeg.ps1").read_text(
        encoding="utf-8"
    )

    assert "tools\\ffmpeg-toolchain.json" in installer
    assert "latest" not in installer.lower()
    assert "Get-FileHash" in installer
    assert "Expand-Archive" in installer
