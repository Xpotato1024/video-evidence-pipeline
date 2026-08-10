from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

from video_evidence_pipeline.storage.layout import is_link_or_junction


@pytest.fixture
def tmp_path() -> Path:
    """Keep Windows test paths short without sharing state between tests."""
    if _TEST_TEMP_ROOT is None:
        raise RuntimeError("test temporary root was not initialized")
    base = _TEST_TEMP_ROOT
    global _TEST_ROOT_NUMBER
    test_root = base / f"t{_TEST_ROOT_NUMBER}"
    _TEST_ROOT_NUMBER += 1
    if test_root.exists():
        raise RuntimeError(f"pytest test root already exists: {test_root}")
    test_root.mkdir()
    return test_root


_TEST_TEMP_ROOT: Path | None = None
_OWNED_TEST_TEMP_ROOT = False
_TEST_ROOT_NUMBER = 0


def _test_temp_root(config) -> Path:
    raw_root = os.environ.get("VEP_TEST_TEMP_ROOT")
    if raw_root:
        candidate = Path(raw_root)
        if not candidate.is_absolute():
            raise pytest.UsageError("VEP_TEST_TEMP_ROOT must be an absolute path")
        candidate = Path(os.path.abspath(os.fspath(candidate)))
        repository_root = Path(config.rootpath).resolve(strict=False)
        reason = _unsafe_basetemp_reason(candidate, repository_root)
        if reason is not None:
            raise pytest.UsageError(f"Unsafe test temporary root rejected: {candidate} is {reason}")
        if not candidate.exists():
            candidate.mkdir(parents=True)
        return _short_windows_path(candidate)

    candidate = Path(os.path.abspath(tempfile.gettempdir()))
    repository_root = Path(config.rootpath).resolve(strict=False)
    reason = _unsafe_basetemp_reason(candidate, repository_root)
    if reason is not None:
        raise pytest.UsageError(f"Unsafe test temporary root rejected: {candidate} is {reason}")

    created = Path(tempfile.mkdtemp(prefix="vep-", dir=str(candidate)))
    return _short_windows_path(created)


def _path_is_within(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.normcase(os.path.abspath(os.fspath(candidate)))
    root_text = os.path.normcase(os.path.abspath(os.fspath(root)))
    try:
        return os.path.commonpath([candidate_text, root_text]) == root_text
    except ValueError:
        return False


def _path_contains_link_or_junction(path: Path) -> bool:
    current = Path(os.path.abspath(os.fspath(path)))
    while True:
        if is_link_or_junction(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _unsafe_basetemp_reason(candidate: Path, repository_root: Path) -> str | None:
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    if absolute == Path(absolute.anchor) or absolute.parent == Path(absolute.anchor):
        return "at a drive or filesystem root, or directly below one"

    if _path_contains_link_or_junction(absolute):
        return "contains a symlink, junction, or reparse point"

    try:
        canonical = absolute.resolve(strict=False)
        canonical_repository = repository_root.resolve(strict=False)
    except OSError:
        return "could not be canonically resolved"

    if _path_is_within(canonical, canonical_repository) or _path_is_within(
        absolute, repository_root
    ):
        return "inside the repository root"

    if absolute.exists() and not absolute.is_dir():
        return "not a directory"

    return None


def _short_windows_path(path: Path) -> Path:
    if os.name != "nt":
        return path

    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        return path
    return Path(buffer.value)


def pytest_sessionstart(session) -> None:
    global _OWNED_TEST_TEMP_ROOT, _TEST_TEMP_ROOT
    _TEST_TEMP_ROOT = _test_temp_root(session.config)
    _OWNED_TEST_TEMP_ROOT = "VEP_TEST_TEMP_ROOT" not in os.environ
    if not _TEST_TEMP_ROOT.exists():
        _TEST_TEMP_ROOT.mkdir(parents=True)


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    global _TEST_TEMP_ROOT
    if _OWNED_TEST_TEMP_ROOT and _TEST_TEMP_ROOT is not None:
        if _TEST_TEMP_ROOT.exists():
            with suppress(OSError):
                shutil.rmtree(_TEST_TEMP_ROOT)
        _TEST_TEMP_ROOT = None


def _resolved_basetemp(config) -> Path | None:
    try:
        raw_basetemp = config.getoption("basetemp")
    except ValueError:
        return None
    if not raw_basetemp:
        return None

    candidate = Path(os.path.abspath(str(raw_basetemp)))
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def pytest_configure(config) -> None:
    basetemp = _resolved_basetemp(config)
    if basetemp is None:
        return

    repository_root = Path(config.rootpath).resolve()
    reason = _unsafe_basetemp_reason(basetemp, repository_root)
    if reason is not None:
        raise pytest.UsageError(
            "Unsafe --basetemp rejected: "
            f"{basetemp} is {reason}. "
            "Use the operating-system temporary directory or an isolated "
            "temporary root outside the repository."
        )
