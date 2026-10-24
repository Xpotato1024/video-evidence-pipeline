"""Offline candidate input integrity and no accidental dependency inventory."""

import base64
import csv
import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "windows_candidate", SCRIPTS / "build_windows_candidate.py"
)
module = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(module)
finally:
    sys.path.pop(0)


@pytest.fixture
def inputs(tmp_path):
    root, packages = tmp_path / "source", tmp_path / "packages"
    root.mkdir()
    packages.mkdir()
    lock = []
    for i in range(21):
        name = f"fixture-{i}"
        info = packages / f"fixture_{i}-1.0.dist-info"
        info.mkdir()
        meta = info / "METADATA"
        data = f"Name: {name}\nVersion: 1.0\n".encode()
        meta.write_bytes(data)
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        with (info / "RECORD").open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(
                [
                    [meta.relative_to(packages).as_posix(), "sha256=" + checksum, str(len(data))],
                    [(info / "RECORD").relative_to(packages).as_posix(), "", ""],
                ]
            )
        lock.append(f'[[package]]\nname="{name}"\nversion="1.0"\n')
    (root / "uv.lock").write_text("\n".join(lock), encoding="utf-8")
    return root, packages


def test_locked_record_inventory_rejects_modified_and_unlisted_files(inputs):
    root, packages = inputs
    assert len(module.verify_packages(root, packages)) == 21
    (packages / ".lock").write_bytes(b"")
    assert len(module.verify_packages(root, packages)) == 21
    extra = packages / "unlisted.py"
    extra.write_bytes(b"unreviewed code")
    with pytest.raises(ValueError, match="unexpected package inventory"):
        module.verify_packages(root, packages)
    extra.unlink()
    (packages / "fixture_0-1.0.dist-info/METADATA").write_bytes(
        b"Name: fixture-0\nVersion: 1.0\nLicense: MIT\n"
    )
    with pytest.raises(ValueError, match="RECORD mismatch"):
        module.verify_packages(root, packages)


def test_record_cannot_escape_target_or_omit_content_hash(inputs):
    root, packages = inputs
    record = packages / "fixture_0-1.0.dist-info/RECORD"
    record.write_text("../outside.py,,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe wheel RECORD path"):
        module.verify_packages(root, packages)
    record.write_text("fixture_0-1.0.dist-info/METADATA,,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unhashed wheel file"):
        module.verify_packages(root, packages)


def test_native_input_requires_bounded_x64_pe_headers(tmp_path):
    path = tmp_path / "synthetic.exe"
    for data in (b"", b"not a binary", b"MZ" + bytes(128)):
        path.write_bytes(data)
        with pytest.raises(ValueError, match="Windows x64 PE"):
            module.verify_pe_x64(path)
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, 0x8664)
    struct.pack_into("<H", data, 88, 0x20B)
    path.write_bytes(data)
    module.verify_pe_x64(path)
    struct.pack_into("<I", data, 60, 0xFFFFFFFF)
    path.write_bytes(data)
    with pytest.raises(ValueError, match="Windows x64 PE"):
        module.verify_pe_x64(path)


def test_untracked_runtime_and_unlisted_notices_are_rejected(tmp_path):
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / "src/included.py").write_bytes(b"public synthetic source")
    module.verify_source_allowlist(root, ["src/included.py"])
    (root / "src/unlisted.py").write_bytes(b"unlisted synthetic source")
    with pytest.raises(ValueError, match="untracked runtime source"):
        module.verify_source_allowlist(root, ["src/included.py"])
    notices = tmp_path / "notices"
    notices.mkdir()
    for name in ("index.json", "THIRD_PARTY_NOTICES.txt", "known.txt"):
        (notices / name).write_bytes(b"synthetic notice")
    dossier = {"notices": [{"path": "known.txt"}]}
    module.verify_notice_inventory(notices, dossier)
    (notices / "unlisted.txt").write_bytes(b"unlisted synthetic notice")
    with pytest.raises(ValueError, match="unlisted notice input"):
        module.verify_notice_inventory(notices, dossier)
