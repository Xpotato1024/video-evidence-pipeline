import csv
import hashlib
import io
import zipfile

import pytest

from scripts.windows_candidate_transforms import (
    configure_private_module_path,
    omit_generated_console_launchers,
)


def fixture(payload, *, recognizable=True):
    (payload / "packages/bin").mkdir(parents=True)
    info = payload / "packages/fixture-1.0.dist-info"
    info.mkdir()
    path = payload / "packages/bin/fixture.exe"
    path.write_bytes(b"MZ" + (b"uv_trampoline" if recognizable else b"unknown-program"))
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("__main__.py", 'print("synthetic-only")')
    (info / "entry_points.txt").write_text(
        "[console_scripts]\nfixture=fixture:main\n", encoding="utf-8"
    )
    original = b"bin/fixture.exe,sha256=synthetic,1\nfixture-1.0.dist-info/RECORD,,\n"
    (info / "RECORD").write_bytes(original)
    return path, info / "RECORD", original


def test_omission_preserves_original_record_and_updates_new_record(tmp_path):
    path, record, original = fixture(tmp_path)
    result = omit_generated_console_launchers(tmp_path)
    assert len(result) == 1 and not path.exists()
    assert (tmp_path / result[0]["original"]).read_bytes() == original
    assert result[0]["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert list(csv.reader(io.StringIO(record.read_text()))) == [
        ["fixture-1.0.dist-info/RECORD", "", ""]
    ]
    assert not path.parent.exists()


def test_unknown_bin_is_rejected_without_mutation(tmp_path):
    path, record, original = fixture(tmp_path, recognizable=False)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unrecognized generated script wrapper"):
        omit_generated_console_launchers(tmp_path)
    assert path.read_bytes() == before and record.read_bytes() == original


@pytest.mark.parametrize(
    "active", ["python313.zip\n.\nimport site\n", "python313.zip\n.\nC:/untrusted\n"]
)
def test_runtime_configuration_refuses_extra_search_paths_before_mutation(tmp_path, active):
    path = tmp_path / "runtime/python313._pth"
    path.parent.mkdir()
    path.write_text(active, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unexpected embedded runtime"):
        configure_private_module_path(tmp_path)
    assert path.read_bytes() == before
    assert not (tmp_path / "packaging").exists()


def test_runtime_configuration_preserves_original_and_site_policy(tmp_path):
    path = tmp_path / "runtime/python313._pth"
    path.parent.mkdir()
    original = b"python313.zip\r\n.\r\n#import site\r\n"
    path.write_bytes(original)
    result = configure_private_module_path(tmp_path)
    assert (tmp_path / result["original"]).read_bytes() == original
    assert result["import_site"] is False
    active = [s for s in path.read_text().splitlines() if s and not s.startswith("#")]
    assert active == ["python313.zip", ".", "../packages", "../payload/src"]
