"""Hardened yt-dlp subprocess execution with private, single-file output."""

from __future__ import annotations

import importlib.metadata
import os
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from video_evidence_pipeline.acquisition.https_proxy import validating_https_proxy
from video_evidence_pipeline.acquisition.network_policy import (
    Resolver,
    default_resolver,
    validate_network_url,
)
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionDeadline,
    AcquisitionRuntimeConfig,
)
from video_evidence_pipeline.acquisition.staged import (
    AcquisitionEvidenceDraft,
    StagedSource,
    staging_directory,
)
from video_evidence_pipeline.domain.identifiers import sanitize_filename
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.manifests import CredentialMode, HttpStatusCategory
from video_evidence_pipeline.errors import integrity_error, io_error
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    enforce_private_file,
    ensure_private_directory,
)

SubprocessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def ytdlp_version() -> str:
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def hardened_argv(
    locator: str,
    output_directory: Path,
    runtime: AcquisitionRuntimeConfig,
    *,
    proxy_url: str,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-js-runtimes",
        "--no-update",
        "--no-playlist",
        "--abort-on-error",
        "--no-cache-dir",
        "--no-write-info-json",
        "--no-write-description",
        "--no-write-comments",
        "--no-write-thumbnail",
        "--no-write-subs",
        "--no-write-auto-subs",
        "--no-write-playlist-metafiles",
        "--no-embed-metadata",
        "--no-embed-info-json",
        "--no-embed-thumbnail",
        "--no-embed-subs",
        "--fixup",
        "never",
        "--no-continue",
        "--no-overwrites",
        "--no-netrc",
        "--no-cookies",
        "--proxy",
        proxy_url,
        "--quiet",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        str(runtime.network_timeout_seconds),
        "--max-filesize",
        str(runtime.maximum_download_bytes),
        "--format",
        "best[acodec!=none][vcodec!=none]",
        "--paths",
        str(output_directory),
        "--output",
        "source.%(ext)s",
    ]
    if runtime.cookies_from_browser is not None:
        argv.extend(["--cookies-from-browser", runtime.cookies_from_browser.value])
    argv.extend(["--", locator])
    return argv


def _subprocess_environment(source: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "APPDATA",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
    environment = {name: value for name, value in source.items() if name.upper() in allowed}
    environment["YTDLP_NO_PLUGINS"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def run_ytdlp(
    locator: str,
    workspace: JobWorkspace,
    runtime: AcquisitionRuntimeConfig,
    *,
    adapter_kind: SourceKind,
    runner: SubprocessRunner = subprocess.run,
    environment: Mapping[str, str] | None = None,
    resolver: Resolver = default_resolver,
    deadline: AcquisitionDeadline | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> StagedSource:
    acquisition_deadline = deadline or AcquisitionDeadline.start(
        runtime.maximum_download_seconds,
        monotonic=monotonic,
    )
    run_directory = staging_directory(workspace) / uuid.uuid4().hex
    ensure_private_directory(run_directory)
    validate_network_url(
        locator,
        resolver=resolver,
        resolve_timeout_seconds=min(
            runtime.network_timeout_seconds,
            acquisition_deadline.remaining(),
        ),
    )
    try:
        with validating_https_proxy(
            resolver=resolver,
            connect_timeout_seconds=runtime.network_timeout_seconds,
            deadline=acquisition_deadline,
        ) as proxy_url:
            argv = hardened_argv(locator, run_directory, runtime, proxy_url=proxy_url)
            try:
                completed = runner(
                    argv,
                    shell=False,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=run_directory,
                    env=_subprocess_environment(environment or os.environ),
                    timeout=acquisition_deadline.remaining(),
                )
            except FileNotFoundError:
                raise io_error("YTDLP_NOT_AVAILABLE", "yt-dlp runtime is not available") from None
            except subprocess.TimeoutExpired:
                raise io_error("YTDLP_ACQUISITION_FAILED", "yt-dlp acquisition timed out") from None
        if completed.returncode != 0:
            raise io_error("YTDLP_ACQUISITION_FAILED", "yt-dlp acquisition failed")
        entries = list(run_directory.rglob("*"))
        if any(entry.is_dir() for entry in entries):
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp created unexpected output")
        if len(entries) != 1:
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp must create exactly one file")
        candidate = entries[0]
        try:
            candidate.relative_to(run_directory)
        except ValueError as exc:
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp output escaped staging") from exc
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp output must not be a link")
        metadata = candidate.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp output is not a regular file")
        if metadata.st_size > runtime.maximum_download_bytes:
            raise integrity_error("SOURCE_TOO_LARGE", "yt-dlp output exceeds the configured limit")
        enforce_private_file(candidate)
        host = (urlsplit(locator).hostname or "").lower()
        return StagedSource(
            path=candidate,
            display_name=sanitize_filename(candidate.name),
            cleanup_root=run_directory,
            evidence=AcquisitionEvidenceDraft(
                adapter_kind=adapter_kind,
                transport="https",
                backend="yt-dlp",
                backend_version=ytdlp_version(),
                requested_host=host,
                final_host=host,
                redirect_count=0,
                http_status_category=HttpStatusCategory.NOT_OBSERVED,
                response_content_type=None,
                declared_content_length=None,
                credential_mode=(
                    CredentialMode.BROWSER_COOKIE
                    if runtime.cookies_from_browser is not None
                    else CredentialMode.NONE
                ),
            ),
        )
    except Exception:
        with suppress(OSError):
            for entry in sorted(run_directory.rglob("*"), reverse=True):
                if entry.is_file() or entry.is_symlink():
                    entry.unlink(missing_ok=True)
                elif entry.is_dir():
                    entry.rmdir()
            run_directory.rmdir()
        raise
