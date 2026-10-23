from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile

import pytest

from scripts.build_private_runtime import build, canonical_destination


def inputs(tmp_path):
    source = tmp_path / "source"
    for name in ("src", ".agents", "config", "schemas", "containers/asr", "native"):
        (source / name).mkdir(parents=True)
    for name in (
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
        "README.md",
        "native/worker_bootstrap.py",
    ):
        (source / name).write_text("synthetic fixture\n", encoding="utf-8")
    packages = tmp_path / "packages"
    packages.mkdir()
    (packages / "synthetic.py").write_text("# fixture\n", encoding="utf-8")
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("python.exe", b"synthetic non-executable fixture")
        bundle.writestr("LICENSE.txt", b"synthetic license fixture")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return source, packages, archive, digest


def test_builder_manifest_is_exclusive_and_dotted_output_names_do_not_collide(tmp_path):
    source, packages, archive, digest = inputs(tmp_path)
    output = tmp_path / "release.r1"
    existing = tmp_path / "release.r1.manifest.json"
    existing.write_bytes(b"existing synthetic manifest\n")
    with pytest.raises(ValueError):
        build(source, archive, digest, packages, output, "r1")
    assert not output.exists()
    assert existing.read_bytes() == b"existing synthetic manifest\n"
    second = tmp_path / "release.r2"
    build(source, archive, digest, packages, second, "r2")
    assert (tmp_path / "release.r2.manifest.json").is_file()
    assert existing.read_bytes() == b"existing synthetic manifest\n"


def test_builder_rejects_input_directory_links_before_copy(tmp_path):
    source, packages, archive, digest = inputs(tmp_path)
    external = tmp_path / "external-fixture"
    external.mkdir()
    sentinel = external / "synthetic-private.txt"
    sentinel.write_bytes(b"must not enter payload\n")
    link = packages / "linked"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            pytest.skip("test junction creation unavailable")
    else:
        link.symlink_to(external, target_is_directory=True)
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="links/reparse"):
        build(source, archive, digest, packages, output, "r1")
    assert not output.exists()
    assert sentinel.read_bytes() == b"must not enter payload\n"


def test_builder_rejects_output_inside_input_before_writing(tmp_path):
    source, packages, archive, digest = inputs(tmp_path)
    for output in (packages / "nested", source / "src/nested"):
        with pytest.raises(ValueError, match="overlap"):
            build(source, archive, digest, packages, output, "r1")
        assert not output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows short-path alias regression")
def test_builder_rejects_short_path_alias_overlap(tmp_path):
    import ctypes
    from pathlib import Path

    source, packages, archive, digest = inputs(tmp_path)
    long_packages = packages.resolve(strict=True)
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(long_packages), buffer, len(buffer)):
        pytest.skip("short path alias unavailable")
    short_packages = Path(buffer.value)
    if str(short_packages).casefold() == str(long_packages).casefold():
        pytest.skip("volume has no distinct short path alias")
    output = short_packages / "not-created"
    with pytest.raises(ValueError, match="overlap"):
        build(source, archive, digest, long_packages, output, "r1")
    assert not output.exists()


def test_builder_rejects_output_link_parent(tmp_path):
    source, packages, archive, digest = inputs(tmp_path)
    target = tmp_path / "output-target"
    target.mkdir()
    link = tmp_path / "output-link"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if result.returncode:
            pytest.skip("test junction creation unavailable")
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="links/reparse"):
        build(source, archive, digest, packages, link / "not-created", "r1")
    assert list(target.iterdir()) == []


def test_canonical_destination_stops_at_inaccessible_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "lexists", lambda _: False)
    with pytest.raises(ValueError, match="accessible existing parent"):
        canonical_destination(tmp_path / "missing")
