"""Hardened yt-dlp subprocess execution with private, single-file output."""

from __future__ import annotations

import importlib.metadata
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from video_evidence_pipeline.acquisition.https_proxy import validating_https_proxy
from video_evidence_pipeline.acquisition.network_policy import (
    Resolver,
    default_resolver,
    validate_network_url,
)
from video_evidence_pipeline.acquisition.panopto_ytdlp_protocol import (
    PANOPTO_CHILD_EXIT_REAUTH_REQUIRED,
    PANOPTO_CHILD_EXIT_VIEWER_DENIED,
)
from video_evidence_pipeline.acquisition.process_tree import ManagedProcessTree
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
from video_evidence_pipeline.domain.locators import panopto_identity
from video_evidence_pipeline.domain.manifests import (
    AcquisitionMode,
    CredentialMode,
    HttpStatusCategory,
    YtdlpFormatEvidence,
)
from video_evidence_pipeline.domain.media import MediaStreamType, NormalizationConfig, ToolIdentity
from video_evidence_pipeline.errors import (
    VepError,
    configuration_error,
    integrity_error,
    io_error,
    state_error,
)
from video_evidence_pipeline.media.probe import probe_media
from video_evidence_pipeline.media.runner import FFmpegRunner
from video_evidence_pipeline.storage.layout import JobWorkspace
from video_evidence_pipeline.storage.permissions import (
    enforce_private_file,
    ensure_private_directory,
)

SubprocessRunner = Callable[..., subprocess.CompletedProcess[bytes]]
ProcessFactory = Callable[..., subprocess.Popen[bytes]]
_FORMAT_SELECTOR = "bv*+ba/b"
_LINEAGE_FILENAME = ".vep-ytdlp-lineage.jsonl"
_LINEAGE_MAXIMUM_BYTES = 16 * 1024
_MAXIMUM_STAGING_ENTRIES = 8
_TOOL_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
_TOOL_DISCOVERY_TIMEOUT_SECONDS = 30.0
_HTTP_RETRIES = 3
_FRAGMENT_RETRIES = 3
_LINEAGE_TEMPLATE = (
    '{"final":%(.{format_id,vcodec,acodec,protocol})j,'
    '"components":%(requested_formats.:.{format_id,vcodec,acodec,protocol})j}'
)


_PANOPTO_LINEAGE_TEMPLATE = '{"media":%(.{id,duration})j,' + _LINEAGE_TEMPLATE[1:]


def check_panopto_duration(expected_seconds: object, actual_ms: int) -> None:
    if expected_seconds is None:
        return
    config = NormalizationConfig()
    if (
        type(expected_seconds) not in {int, float}
        or not 0 < expected_seconds <= config.maximum_duration_ms / 1000
        or abs(actual_ms - expected_seconds * 1000) > config.duration_tolerance_ms
    ):
        raise integrity_error("PANOPTO_DURATION_MISMATCH", "Panopto media duration did not match")


class YtdlpToolchain(Protocol):
    ffmpeg_identity: ToolIdentity
    ffmpeg_location: Path

    def validate_final(self, path: Path, deadline: AcquisitionDeadline) -> int: ...

    def revalidate(self) -> None: ...


YtdlpToolchainFactory = Callable[[AcquisitionDeadline], YtdlpToolchain]


@dataclass(frozen=True, slots=True)
class _PinnedYtdlpToolchain:
    runner: FFmpegRunner
    ffmpeg_identity: ToolIdentity
    ffprobe_identity: ToolIdentity
    ffmpeg_location: Path

    def validate_final(self, path: Path, deadline: AcquisitionDeadline) -> int:
        validation_error: VepError | None = None
        try:
            probe = probe_media(
                self.runner,
                path,
                NormalizationConfig(
                    tool_timeout_seconds=min(
                        _TOOL_DISCOVERY_TIMEOUT_SECONDS,
                        deadline.remaining(),
                    )
                ),
            )
        except VepError as exc:
            if exc.code == "NETWORK_TIMEOUT":
                raise
            if exc.code == "MEDIA_TOOL_TIMEOUT":
                deadline.remaining()
            if exc.code in {
                "MEDIA_TOOL_IDENTITY_CHANGED",
                "MEDIA_TOOL_IDENTITY_INVALID",
                "MEDIA_TOOL_INVALID",
                "MEDIA_TOOL_NOT_AVAILABLE",
                "MEDIA_TOOL_PIPE_FAILED",
                "MEDIA_TOOL_START_FAILED",
            }:
                validation_error = configuration_error(
                    "YTDLP_TOOLCHAIN_INVALID", "yt-dlp media toolchain validation failed"
                )
            else:
                validation_error = integrity_error(
                    "YTDLP_OUTPUT_INVALID", "yt-dlp final media could not be verified"
                )
        if validation_error is not None:
            raise validation_error
        video_count = sum(stream.stream_type is MediaStreamType.VIDEO for stream in probe.streams)
        audio_count = sum(stream.stream_type is MediaStreamType.AUDIO for stream in probe.streams)
        if video_count != 1 or audio_count != 1:
            raise integrity_error(
                "YTDLP_OUTPUT_INVALID",
                "yt-dlp final media must contain exactly one video and one audio stream",
            )

        return probe.duration_ms

    def revalidate(self) -> None:
        identity_error = None
        try:
            self.runner.revalidate_pinned_identities(
                self.ffmpeg_identity,
                self.ffprobe_identity,
            )
        except VepError:
            identity_error = configuration_error(
                "YTDLP_TOOLCHAIN_INVALID", "yt-dlp media toolchain identity changed"
            )
        if identity_error is not None:
            raise identity_error


def default_ytdlp_toolchain(deadline: AcquisitionDeadline) -> YtdlpToolchain:
    toolchain_error = None
    try:
        runner = FFmpegRunner()
        ffmpeg, ffprobe = runner.discover_identities(
            timeout_seconds=_TOOL_DISCOVERY_TIMEOUT_SECONDS,
            output_limit_bytes=_TOOL_OUTPUT_LIMIT_BYTES,
            remaining_seconds=deadline.remaining,
        )
        location = runner.pinned_bin_directory(ffmpeg, ffprobe)
    except VepError as exc:
        if exc.code == "NETWORK_TIMEOUT":
            raise
        if exc.code == "MEDIA_TOOL_TIMEOUT":
            # A discovery subprocess can consume the exact remaining absolute
            # budget. Preserve deadline exhaustion rather than reclassifying it.
            deadline.remaining()
        toolchain_error = configuration_error(
            "YTDLP_TOOLCHAIN_INVALID", "yt-dlp requires a validated media toolchain"
        )
    if toolchain_error is not None:
        raise toolchain_error
    return _PinnedYtdlpToolchain(runner, ffmpeg, ffprobe, location)


def _staging_entries(output_directory: Path) -> list[tuple[Path, os.stat_result]]:
    entries: list[tuple[Path, os.stat_result]] = []
    try:
        children = list(os.scandir(output_directory))
    except OSError:
        children = None
    if children is None:
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp staging could not be inspected")
    if len(children) > _MAXIMUM_STAGING_ENTRIES:
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp created too many staging entries")
    for child in children:
        path = Path(child.path)
        metadata = None
        try:
            if child.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise integrity_error(
                    "YTDLP_OUTPUT_INVALID", "yt-dlp staging must not contain links"
                )
            metadata = child.stat(follow_symlinks=False)
        except OSError:
            metadata = None
        if metadata is None:
            raise integrity_error(
                "YTDLP_OUTPUT_INVALID", "yt-dlp staging entry could not be verified"
            )
        if stat.S_ISDIR(metadata.st_mode):
            raise integrity_error(
                "YTDLP_OUTPUT_INVALID", "yt-dlp staging must not contain directories"
            )
        entries.append((path, metadata))
    return entries


def _staged_output_size(output_directory: Path, *, maximum_component_bytes: int) -> int:
    total = 0
    for entry, metadata in _staging_entries(output_directory):
        if stat.S_ISREG(metadata.st_mode):
            maximum = (
                _LINEAGE_MAXIMUM_BYTES
                if entry.name == _LINEAGE_FILENAME
                else maximum_component_bytes
            )
            if metadata.st_size > maximum:
                raise integrity_error(
                    "SOURCE_TOO_LARGE",
                    "yt-dlp staging output exceeded a configured per-file limit",
                )
            if entry.name != _LINEAGE_FILENAME:
                total += metadata.st_size
    return total


def _cleanup_run_directory(run_directory: Path) -> None:
    cleanup_failed = False
    pending = [run_directory]
    directories: list[Path] = []
    while pending:
        directory = pending.pop()
        directories.append(directory)
        try:
            children = list(os.scandir(directory))
        except OSError:
            cleanup_failed = True
            continue
        for child in children:
            path = Path(child.path)
            try:
                is_link = child.is_symlink() or getattr(path, "is_junction", lambda: False)()
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode) and not is_link:
                    pending.append(path)
                elif stat.S_ISDIR(metadata.st_mode):
                    path.rmdir()
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            cleanup_failed = True
    if cleanup_failed or run_directory.exists():
        raise io_error(
            "SOURCE_STAGING_CLEANUP_FAILED", "yt-dlp staging cleanup could not be completed"
        )


def _lineage_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "format_id",
        "vcodec",
        "acodec",
        "protocol",
    }:
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp lineage is malformed")
    if any(not isinstance(item, str) for item in value.values()):
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp lineage is malformed")
    return value


def _format_evidence(value: Any) -> YtdlpFormatEvidence:
    item = _lineage_mapping(value)
    evidence = None
    with suppress(ValueError):
        evidence = YtdlpFormatEvidence(
            format_id=item["format_id"],
            video_codec=item["vcodec"],
            audio_codec=item["acodec"],
            protocol=item["protocol"],
        )
    if evidence is None:
        raise integrity_error(
            "YTDLP_OUTPUT_INVALID", "yt-dlp lineage contains unsafe format metadata"
        )
    return evidence


def _parse_lineage(
    path: Path, expected_panopto_id: str | None
) -> tuple[AcquisitionMode, tuple[YtdlpFormatEvidence, ...], object]:
    lines = None
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise OSError("lineage path is a link")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            payload_bytes = stream.read(_LINEAGE_MAXIMUM_BYTES + 1)
            after = os.fstat(stream.fileno())
        if not stat.S_ISREG(after.st_mode) or len(payload_bytes) > _LINEAGE_MAXIMUM_BYTES:
            raise OSError("lineage file is invalid")
        before_state = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_state = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_state != after_state or after.st_size != len(payload_bytes):
            raise OSError("lineage file changed during validation")
        lines = payload_bytes.decode("utf-8").splitlines()
    except (OSError, UnicodeError):
        pass
    if lines is None:
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp lineage could not be verified")
    if len(lines) != 1 or not lines[0]:
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp lineage is malformed")
    payload = None
    with suppress(json.JSONDecodeError):
        payload = json.loads(lines[0])
    if payload is None:
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp lineage is malformed")
    duration = None
    if expected_panopto_id is not None and isinstance(payload, dict):
        media = payload.pop("media", None)
        if (
            not isinstance(media, dict)
            or media.get("id") != expected_panopto_id
            or set(media) - {"id", "duration"}
        ):
            raise integrity_error(
                "PANOPTO_VIEWER_ID_MISMATCH", "viewer lecture identity did not match"
            )
        duration = media.get("duration")
    if not isinstance(payload, dict) or set(payload) != {"final", "components"}:
        raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp lineage is malformed")
    components = payload["components"]
    final_metadata = payload["final"]
    if (
        expected_panopto_id is not None
        and components == []
        and isinstance(final_metadata, dict)
        and final_metadata.get("protocol") == "https"
    ):
        # Panopto's progressive MP4 listing may omit codec names. The final
        # file must independently pass the one-video/one-audio probe below.
        final_metadata = {"vcodec": "unknown", "acodec": "unknown", **final_metadata}
    final = _format_evidence(final_metadata)
    if components == []:
        if final.video_codec == "none" or final.audio_codec == "none":
            raise integrity_error(
                "YTDLP_FORMAT_UNAVAILABLE", "yt-dlp did not select a combined audio/video format"
            )
        return AcquisitionMode.PROGRESSIVE, (final,), duration
    if not isinstance(components, list) or len(components) != 2:
        raise integrity_error(
            "YTDLP_FORMAT_UNAVAILABLE", "yt-dlp selected an unsupported stream composition"
        )
    selected = tuple(_format_evidence(item) for item in components)
    if not (
        selected[0].video_codec != "none"
        and selected[0].audio_codec == "none"
        and selected[1].video_codec == "none"
        and selected[1].audio_codec != "none"
    ):
        raise integrity_error(
            "YTDLP_FORMAT_UNAVAILABLE", "yt-dlp selected an unsupported stream composition"
        )
    return AcquisitionMode.ADAPTIVE_MERGED, selected, duration


def _last_fragment_index(path: Path) -> int | None:
    # The pinned downloader's private .ytdl file records an index, not a
    # validated byte prefix. Use it only to report where the run stopped.
    try:
        with path.open("rb") as stream:
            data = stream.read(_LINEAGE_MAXIMUM_BYTES + 1)
        if len(data) > _LINEAGE_MAXIMUM_BYTES:
            return None
        index = json.loads(data)["downloader"]["current_fragment"]["index"]
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None
    return index if type(index) is int and 0 <= index <= 2**31 - 1 else None


def _failed_run_error(run_directory: Path, *, timed_out: bool = False) -> VepError:
    try:
        entries = _staging_entries(run_directory)
        media_files = [
            entry
            for entry, metadata in entries
            if stat.S_ISREG(metadata.st_mode)
            and entry.name != _LINEAGE_FILENAME
            and ".part" not in entry.name
            and not entry.name.endswith(".ytdl")
        ]
    except VepError as exc:
        return exc
    if not timed_out:
        progress = [
            index
            for path, metadata in entries
            if stat.S_ISREG(metadata.st_mode)
            and path.name.startswith("source.")
            and path.suffix == ".ytdl"
            and (index := _last_fragment_index(path)) is not None
        ]
        if progress:
            message = "Segmented acquisition did not complete"
            if len(progress) == 1:
                message += f" (last recorded fragment index: {progress[0]})"
            return io_error("YTDLP_SEGMENTED_INCOMPLETE", message)
    if len(media_files) >= 2:
        return io_error(
            "YTDLP_MERGE_FAILED",
            "yt-dlp adaptive merge timed out" if timed_out else "yt-dlp adaptive merge failed",
        )
    return io_error(
        "YTDLP_ACQUISITION_FAILED",
        "yt-dlp acquisition timed out" if timed_out else "yt-dlp acquisition failed",
    )


def _run_monitored_ytdlp(
    argv: list[str],
    *,
    output_directory: Path,
    maximum_component_bytes: int,
    maximum_staging_bytes: int,
    deadline: AcquisitionDeadline,
    environment: Mapping[str, str],
    process_factory: ProcessFactory,
) -> subprocess.CompletedProcess[bytes]:
    launch_error = None
    try:
        process = ManagedProcessTree.start(
            argv,
            process_factory=process_factory,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=output_directory,
            env=environment,
        )
    except (OSError, ValueError):
        launch_error = io_error("YTDLP_NOT_AVAILABLE", "yt-dlp runtime is not available")
    if launch_error is not None:
        raise launch_error
    try:
        while True:
            if (
                _staged_output_size(
                    output_directory,
                    maximum_component_bytes=maximum_component_bytes,
                )
                > maximum_staging_bytes
            ):
                raise integrity_error(
                    "SOURCE_TOO_LARGE",
                    "yt-dlp cumulative staging exceeded the configured limit",
                )
            returncode = process.poll()
            if returncode is not None:
                process.close()
                return subprocess.CompletedProcess(argv, returncode)
            remaining = deadline.remaining()
            time.sleep(min(0.01, remaining))
    except Exception:
        process.stop()
        raise


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
    ffmpeg_location: Path,
    lineage_path: Path,
    adapter_kind: SourceKind = SourceKind.GENERIC_YTDLP,
) -> list[str]:
    template = (
        _PANOPTO_LINEAGE_TEMPLATE if adapter_kind is SourceKind.PANOPTO else _LINEAGE_TEMPLATE
    )
    module = (
        "video_evidence_pipeline.acquisition.panopto_ytdlp_child"
        if adapter_kind is SourceKind.PANOPTO
        else "yt_dlp"
    )
    argv = [
        sys.executable,
        "-m",
        module,
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-js-runtimes",
        "--no-update",
        "--no-playlist",
        "--abort-on-error",
        "--abort-on-unavailable-fragments",
        "--retries",
        str(_HTTP_RETRIES),
        "--fragment-retries",
        str(_FRAGMENT_RETRIES),
        "--concurrent-fragments",
        "1",
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
        "--no-keep-video",
        "--no-video-multistreams",
        "--no-audio-multistreams",
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
        "--ffmpeg-location",
        str(ffmpeg_location),
        "--format",
        _FORMAT_SELECTOR,
        "--print-to-file",
        f"after_move:{template}",
        str(lineage_path),
        "--paths",
        str(output_directory),
        "--output",
        "source.%(ext)s",
    ]
    if adapter_kind is SourceKind.PANOPTO:
        argv.append("--no-mark-watched")
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
    expected_duration_seconds: object = None,
    runner: SubprocessRunner | None = None,
    environment: Mapping[str, str] | None = None,
    resolver: Resolver = default_resolver,
    deadline: AcquisitionDeadline | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    process_factory: ProcessFactory = subprocess.Popen,
    toolchain_factory: YtdlpToolchainFactory = default_ytdlp_toolchain,
) -> StagedSource:
    acquisition_deadline = deadline or AcquisitionDeadline.start(
        runtime.maximum_download_seconds,
        monotonic=monotonic,
    )
    run_directory = staging_directory(workspace) / uuid.uuid4().hex
    ensure_private_directory(run_directory)
    try:
        toolchain = toolchain_factory(acquisition_deadline)
        if not toolchain.ffmpeg_location.is_absolute():
            raise configuration_error(
                "YTDLP_TOOLCHAIN_INVALID", "yt-dlp FFmpeg location must be absolute"
            )
        lineage_path = run_directory / _LINEAGE_FILENAME
        validate_network_url(
            locator,
            resolver=resolver,
            resolve_timeout_seconds=min(
                runtime.network_timeout_seconds,
                acquisition_deadline.remaining(),
            ),
        )
        with validating_https_proxy(
            resolver=resolver,
            connect_timeout_seconds=runtime.network_timeout_seconds,
            deadline=acquisition_deadline,
        ) as proxy_url:
            argv = hardened_argv(
                locator,
                run_directory,
                runtime,
                proxy_url=proxy_url,
                ffmpeg_location=toolchain.ffmpeg_location,
                lineage_path=lineage_path,
                adapter_kind=adapter_kind,
            )
            subprocess_environment = _subprocess_environment(environment or os.environ)
            maximum_staging_bytes = 3 * runtime.maximum_download_bytes
            if runner is None:
                completed = _run_monitored_ytdlp(
                    argv,
                    output_directory=run_directory,
                    maximum_component_bytes=runtime.maximum_download_bytes,
                    maximum_staging_bytes=maximum_staging_bytes,
                    deadline=acquisition_deadline,
                    environment=subprocess_environment,
                    process_factory=process_factory,
                )
            else:
                runner_error = None
                try:
                    completed = runner(
                        argv,
                        shell=False,
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        cwd=run_directory,
                        env=subprocess_environment,
                        timeout=acquisition_deadline.remaining(),
                    )
                except (OSError, ValueError):
                    runner_error = io_error(
                        "YTDLP_NOT_AVAILABLE", "yt-dlp runtime is not available"
                    )
                except subprocess.TimeoutExpired:
                    runner_error = _failed_run_error(run_directory, timed_out=True)
                if runner_error is not None:
                    raise runner_error
        toolchain.revalidate()
        if completed.returncode != 0:
            if adapter_kind is SourceKind.PANOPTO:
                if completed.returncode == PANOPTO_CHILD_EXIT_REAUTH_REQUIRED:
                    raise state_error(
                        "PANOPTO_REAUTH_REQUIRED",
                        "Panopto viewer requires browser reauthentication",
                    )
                if completed.returncode == PANOPTO_CHILD_EXIT_VIEWER_DENIED:
                    raise state_error(
                        "PANOPTO_VIEWER_DENIED",
                        "Panopto denied access to the requested viewer session",
                    )
            raise _failed_run_error(run_directory)
        if (
            _staged_output_size(
                run_directory,
                maximum_component_bytes=runtime.maximum_download_bytes,
            )
            > maximum_staging_bytes
        ):
            raise integrity_error(
                "SOURCE_TOO_LARGE", "yt-dlp cumulative staging exceeded the configured limit"
            )
        session_id = panopto_identity(locator)[1] if adapter_kind is SourceKind.PANOPTO else None
        acquisition_mode, selected_formats, expected_duration = _parse_lineage(
            lineage_path, session_id
        )
        lineage_cleanup_error = None
        try:
            lineage_path.unlink()
        except OSError:
            lineage_cleanup_error = integrity_error(
                "YTDLP_OUTPUT_INVALID", "yt-dlp lineage could not be removed"
            )
        if lineage_cleanup_error is not None:
            raise lineage_cleanup_error
        entries = _staging_entries(run_directory)
        if len(entries) != 1:
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp must create exactly one file")
        candidate, candidate_metadata = entries[0]
        if not candidate.is_relative_to(run_directory):
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp output escaped staging")
        if not stat.S_ISREG(candidate_metadata.st_mode) or candidate_metadata.st_size <= 0:
            raise integrity_error("YTDLP_OUTPUT_INVALID", "yt-dlp output is not a regular file")
        if candidate_metadata.st_size > runtime.maximum_download_bytes:
            raise integrity_error("SOURCE_TOO_LARGE", "yt-dlp output exceeds the configured limit")
        enforce_private_file(candidate)
        duration_ms = toolchain.validate_final(candidate, acquisition_deadline)
        if session_id is not None:
            check_panopto_duration(expected_duration, duration_ms)
            check_panopto_duration(expected_duration_seconds, duration_ms)
        toolchain.revalidate()
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
                final_host=None,
                redirect_count=None,
                http_status_category=HttpStatusCategory.NOT_OBSERVED,
                response_content_type=None,
                declared_content_length=None,
                credential_mode=(
                    CredentialMode.BROWSER_COOKIE
                    if runtime.cookies_from_browser is not None
                    else CredentialMode.NONE
                ),
                acquisition_mode=acquisition_mode,
                selected_formats=selected_formats,
                merger=(
                    toolchain.ffmpeg_identity
                    if acquisition_mode is AcquisitionMode.ADAPTIVE_MERGED
                    else None
                ),
                warnings=("yt-dlp final host and redirect count were not observed",),
            ),
        )
    except Exception as exc:
        operation_error = exc
    _cleanup_run_directory(run_directory)
    raise operation_error
