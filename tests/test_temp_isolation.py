from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import conftest as temp_policy
import pytest


def _config(repository_root: Path, basetemp: Path | None = None) -> SimpleNamespace:
    def getoption(name: str) -> str | None:
        if name != "basetemp":
            raise ValueError(name)
        return str(basetemp) if basetemp is not None else None

    return SimpleNamespace(rootpath=repository_root, getoption=getoption)


def test_default_os_temp_validates_before_mkdtemp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    unsafe_parent = repository_root / "unsafe-temp"
    calls: list[dict[str, object]] = []

    monkeypatch.delenv("VEP_TEST_TEMP_ROOT", raising=False)
    monkeypatch.setattr(temp_policy.tempfile, "gettempdir", lambda: str(unsafe_parent))

    def fail_mkdtemp(**kwargs: object) -> str:
        calls.append(kwargs)
        raise AssertionError("mkdtemp must not run for an unsafe parent")

    monkeypatch.setattr(temp_policy.tempfile, "mkdtemp", fail_mkdtemp)

    with pytest.raises(pytest.UsageError):
        temp_policy._test_temp_root(_config(repository_root))

    assert calls == []
    assert not unsafe_parent.exists()


def test_default_os_temp_outside_repository_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.delenv("VEP_TEST_TEMP_ROOT", raising=False)
    monkeypatch.setattr(temp_policy.tempfile, "gettempdir", lambda: str(os_temp))

    created = temp_policy._test_temp_root(_config(repository_root))
    try:
        assert created.exists()
        assert created.resolve().parent == os_temp.resolve()
    finally:
        shutil.rmtree(created)


@pytest.mark.skipif(os.name == "nt", reason="POSIX system temp root contract")
def test_default_posix_system_temp_parent_is_allowed_before_mkdtemp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    created = tmp_path / "created-by-mkdtemp"
    calls: list[dict[str, object]] = []

    monkeypatch.delenv("VEP_TEST_TEMP_ROOT", raising=False)
    monkeypatch.setattr(temp_policy.tempfile, "gettempdir", lambda: "/tmp")

    def record_mkdtemp(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(created)

    monkeypatch.setattr(temp_policy.tempfile, "mkdtemp", record_mkdtemp)

    result = temp_policy._test_temp_root(_config(repository_root))

    assert result == created
    assert calls == [{"prefix": "vep-", "dir": "/tmp"}]


def test_explicit_safe_temp_root_is_created_after_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    configured = tmp_path / "safe-temp" / "nested"
    assert not configured.exists()

    monkeypatch.setenv("VEP_TEST_TEMP_ROOT", str(configured))
    result = temp_policy._test_temp_root(_config(repository_root))

    assert result.exists()
    assert result.resolve() == configured.resolve()


def test_relative_configured_temp_root_is_rejected_before_mkdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    relative = "relative-test-temp-root"
    candidate = Path.cwd() / relative
    monkeypatch.setenv("VEP_TEST_TEMP_ROOT", relative)

    with pytest.raises(pytest.UsageError):
        temp_policy._test_temp_root(_config(repository_root))

    assert not candidate.exists()


@pytest.mark.parametrize("candidate_name", ["repository", "repository/child"])
def test_repository_root_and_child_are_rejected_before_mkdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidate_name: str
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    candidate = tmp_path / candidate_name
    if candidate_name.endswith("child"):
        assert not candidate.exists()
    monkeypatch.setenv("VEP_TEST_TEMP_ROOT", str(candidate))

    with pytest.raises(pytest.UsageError):
        temp_policy._test_temp_root(_config(repository_root))

    if candidate_name.endswith("child"):
        assert not candidate.exists()


def test_drive_or_filesystem_root_is_rejected_before_mkdir(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    reason = temp_policy._unsafe_basetemp_reason(Path(tmp_path.anchor), repository_root)

    assert reason is not None


def test_symlink_alias_to_repository_is_rejected(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    alias = tmp_path / "safe-looking-alias"
    try:
        alias.symlink_to(repository_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    reason = temp_policy._unsafe_basetemp_reason(alias, repository_root)

    assert reason is not None


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-specific")
def test_junction_alias_to_repository_is_rejected(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    alias = tmp_path / "safe-looking-junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(alias), str(repository_root)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0 or not getattr(alias, "is_junction", lambda: False)():
        pytest.skip("junction creation is unavailable")

    try:
        assert temp_policy._unsafe_basetemp_reason(alias, repository_root) is not None
    finally:
        alias.rmdir()


def test_safe_basetemp_is_accepted_without_creation(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    safe_basetemp = tmp_path / "safe-basetemp"

    temp_policy.pytest_configure(_config(repository_root, safe_basetemp))

    assert not safe_basetemp.exists()


@pytest.mark.parametrize("candidate", ["repository/child", "root-near"])
def test_unsafe_basetemp_is_rejected_without_creation(tmp_path: Path, candidate: str) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    if candidate == "root-near":
        basetemp = Path(tmp_path.anchor) / "vep-unsafe-basetemp"
    else:
        basetemp = repository_root / "child"

    with pytest.raises(pytest.UsageError):
        temp_policy.pytest_configure(_config(repository_root, basetemp))

    assert not basetemp.exists()


def test_owned_temp_root_cleanup(tmp_path: Path) -> None:
    candidate = tmp_path / "owned-temp"
    candidate.mkdir()
    previous_root = temp_policy._TEST_TEMP_ROOT
    previous_owned = temp_policy._OWNED_TEST_TEMP_ROOT
    temp_policy._TEST_TEMP_ROOT = candidate
    temp_policy._OWNED_TEST_TEMP_ROOT = True
    try:
        temp_policy.pytest_sessionfinish(None, 0)
        assert not candidate.exists()
    finally:
        temp_policy._TEST_TEMP_ROOT = previous_root
        temp_policy._OWNED_TEST_TEMP_ROOT = previous_owned


def test_cleanup_failure_does_not_touch_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    candidate = tmp_path / "owned-temp"
    candidate.mkdir()
    before = tuple(repository_root.iterdir())
    previous_root = temp_policy._TEST_TEMP_ROOT
    previous_owned = temp_policy._OWNED_TEST_TEMP_ROOT
    temp_policy._TEST_TEMP_ROOT = candidate
    temp_policy._OWNED_TEST_TEMP_ROOT = True

    def fail_rmtree(_path: Path) -> None:
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(temp_policy.shutil, "rmtree", fail_rmtree)
    try:
        temp_policy.pytest_sessionfinish(None, 0)
        assert candidate.exists()
        assert tuple(repository_root.iterdir()) == before
    finally:
        temp_policy._TEST_TEMP_ROOT = previous_root
        temp_policy._OWNED_TEST_TEMP_ROOT = previous_owned
