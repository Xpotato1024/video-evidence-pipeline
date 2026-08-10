"""Bounded, non-shell FFmpeg and ffprobe process execution."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.media import ToolIdentity
from video_evidence_pipeline.errors import VepError, configuration_error, io_error

ToolName = Literal["ffmpeg", "ffprobe"]


class _Process(Protocol):
    stdout: BinaryIO | None
    stderr: BinaryIO | None
    returncode: int | None

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


PopenFactory = Callable[..., _Process]


def _listing_names(payload: bytes, *, single_column: bool = False) -> set[str]:
    names: set[str] = set()
    section: str | None = None
    for raw_line in payload.decode("utf-8", errors="replace").splitlines():
        stripped = raw_line.strip()
        if stripped in {"Input:", "Output:"}:
            section = stripped[:-1].lower()
            continue
        tokens = stripped.split()
        if single_column:
            if section is not None and len(tokens) == 1:
                names.add(tokens[0])
            continue
        if (
            len(tokens) >= 2
            and len(tokens[0]) <= 8
            and all(character.isalpha() or character in {".", "="} for character in tokens[0])
        ):
            names.update(tokens[1].split(","))
    return names


def _has_generic_decode_limit(payload: bytes, *, option: str, description: str) -> bool:
    """Require the generic decoder option, not a codec-specific lookalike."""
    for line in payload.decode("utf-8", errors="replace").splitlines():
        parts = line.split(maxsplit=3)
        if (
            len(parts) == 4
            and parts[0] == option
            and parts[1] == "<int64>"
            and parts[3].startswith(description)
        ):
            return True
    return False


def _has_required_decode_limits(payload: bytes) -> bool:
    return _has_generic_decode_limit(
        payload, option="-max_pixels", description="Maximum number of pixels"
    ) and _has_generic_decode_limit(
        payload, option="-max_samples", description="Maximum number of samples"
    )


@dataclass(frozen=True, slots=True)
class ToolRunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    elapsed_seconds: float


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False
        self.error: Exception | None = None

    def consume(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                remaining = self._limit - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        except Exception as exc:
            self.error = exc

    @property
    def value(self) -> bytes:
        return bytes(self._buffer)


def _system_executable(name: ToolName) -> str:
    configured_bin_dir = os.environ.get("VEP_FFMPEG_BIN_DIR")
    if configured_bin_dir is not None:
        if not configured_bin_dir or not Path(configured_bin_dir).is_absolute():
            raise configuration_error(
                "MEDIA_TOOL_INVALID", "configured media tool directory is invalid"
            )
        filename = f"{name}.exe" if os.name == "nt" else name
        return _validate_executable_path(name, Path(configured_bin_dir) / filename)

    resolved = shutil.which(name)
    if resolved is None:
        raise configuration_error(
            "MEDIA_TOOL_NOT_AVAILABLE", f"required media tool is unavailable: {name}"
        )
    return _validate_executable_path(name, resolved)


def _validate_executable_path(name: ToolName, value: str | Path) -> str:
    text = str(value)
    if not text or "\x00" in text:
        raise configuration_error("MEDIA_TOOL_INVALID", "media tool executable is invalid")
    path = Path(text)
    if path.name.lower() not in {name, f"{name}.exe"}:
        raise configuration_error("MEDIA_TOOL_INVALID", "media tool executable name is invalid")
    try:
        if not path.is_file() or path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise configuration_error(
                "MEDIA_TOOL_INVALID", "media tool executable must be a regular non-link file"
            )
    except OSError:
        raise configuration_error(
            "MEDIA_TOOL_INVALID", "media tool executable could not be verified"
        ) from None
    return str(path.absolute())


@dataclass(frozen=True, slots=True)
class _ExecutableSnapshot:
    sha256: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def _stable_executable_snapshot(path: Path) -> _ExecutableSnapshot:
    """Hash a regular executable only when its metadata remains stable at both edges."""
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise OSError("media tool executable must not be a link or junction")
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("media tool executable must be a regular file")
        digest = sha256_file(path)
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise OSError("media tool executable became a link or junction")
        after = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(after.st_mode):
            raise OSError("media tool executable is no longer a regular file")
    except OSError:
        raise configuration_error(
            "MEDIA_TOOL_IDENTITY_INVALID", "media tool executable identity is unavailable"
        ) from None
    before_state = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_state = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_state != after_state:
        raise configuration_error(
            "MEDIA_TOOL_IDENTITY_CHANGED", "media tool changed while its identity was verified"
        )
    return _ExecutableSnapshot(digest, *after_state)


class FFmpegRunner:
    """Run only system ffmpeg/ffprobe with fixed process safety controls."""

    def __init__(
        self,
        *,
        ffmpeg_executable: str | Path | None = None,
        ffprobe_executable: str | Path | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
    ) -> None:
        self._executables: dict[ToolName, str] = {
            "ffmpeg": (
                _system_executable("ffmpeg")
                if ffmpeg_executable is None
                else _validate_executable_path("ffmpeg", ffmpeg_executable)
            ),
            "ffprobe": (
                _system_executable("ffprobe")
                if ffprobe_executable is None
                else _validate_executable_path("ffprobe", ffprobe_executable)
            ),
        }
        self._popen_factory = popen_factory
        self._pinned_snapshots: dict[ToolName, _ExecutableSnapshot] | None = None

    def _snapshots(self) -> dict[ToolName, _ExecutableSnapshot]:
        return {
            tool: _stable_executable_snapshot(Path(path))
            for tool, path in self._executables.items()
        }

    @staticmethod
    def _expected_hashes(
        ffmpeg: ToolIdentity,
        ffprobe: ToolIdentity,
    ) -> dict[ToolName, str]:
        if ffmpeg.name != "ffmpeg" or ffprobe.name != "ffprobe":
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_INVALID", "media tool identity is unavailable"
            )
        return {
            "ffmpeg": ffmpeg.executable_sha256,
            "ffprobe": ffprobe.executable_sha256,
        }

    def pin_identities(self, ffmpeg: ToolIdentity, ffprobe: ToolIdentity) -> None:
        """Establish the identity boundary used at every later subprocess edge."""
        snapshots = self._snapshots()
        if {tool: snapshot.sha256 for tool, snapshot in snapshots.items()} != self._expected_hashes(
            ffmpeg, ffprobe
        ):
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_CHANGED", "media tool changed after capability discovery"
            )
        self._pinned_snapshots = snapshots

    def revalidate_pinned_identities(self, ffmpeg: ToolIdentity, ffprobe: ToolIdentity) -> None:
        """Reject replacement after all media subprocesses and before publication."""
        expected_hashes = self._expected_hashes(ffmpeg, ffprobe)
        if self._pinned_snapshots is None:
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_INVALID", "media tool identity was not pinned"
            )
        snapshots = self._snapshots()
        if (
            snapshots != self._pinned_snapshots
            or {tool: snapshot.sha256 for tool, snapshot in snapshots.items()} != expected_hashes
        ):
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_CHANGED", "media tool changed during normalization"
            )

    def _revalidate_launch_identity(self, tool: ToolName) -> None:
        """Hash at both subprocess edges once identities have been pinned."""
        if self._pinned_snapshots is None:
            return
        try:
            current = _stable_executable_snapshot(Path(self._executables[tool]))
        except VepError:
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_CHANGED", "media tool changed during normalization"
            ) from None
        if current != self._pinned_snapshots[tool]:
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_CHANGED", "media tool changed during normalization"
            )

    def run(
        self,
        tool: ToolName,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
    ) -> ToolRunResult:
        if timeout_seconds <= 0 or stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise configuration_error("MEDIA_TOOL_LIMIT_INVALID", "media tool limits are invalid")
        normalized_arguments = tuple(str(argument) for argument in arguments)
        if any("\x00" in argument for argument in normalized_arguments):
            raise configuration_error(
                "MEDIA_TOOL_ARGUMENT_INVALID", "media tool argument is invalid"
            )
        actual_argv = (self._executables[tool], *normalized_arguments)
        safe_argv = (tool, *normalized_arguments)
        started = time.monotonic()
        self._revalidate_launch_identity(tool)
        try:
            process = self._popen_factory(
                list(actual_argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, ValueError):
            raise io_error("MEDIA_TOOL_START_FAILED", "media tool could not be started") from None
        try:
            stdout_capture = _BoundedCapture(stdout_limit_bytes)
            stderr_capture = _BoundedCapture(stderr_limit_bytes)
            threads: list[threading.Thread] = []
            for stream, capture in (
                (process.stdout, stdout_capture),
                (process.stderr, stderr_capture),
            ):
                if stream is None:
                    process.kill()
                    process.wait()
                    raise io_error("MEDIA_TOOL_START_FAILED", "media tool pipes are unavailable")
                thread = threading.Thread(target=capture.consume, args=(stream,), daemon=True)
                thread.start()
                threads.append(thread)

            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise io_error(
                    "MEDIA_TOOL_TIMEOUT", "media tool exceeded its wall-clock timeout"
                ) from None
            finally:
                for thread in threads:
                    thread.join(timeout=1)

            if (
                any(thread.is_alive() for thread in threads)
                or stdout_capture.error
                or stderr_capture.error
            ):
                raise io_error("MEDIA_TOOL_PIPE_FAILED", "media tool output could not be drained")
            return ToolRunResult(
                argv=safe_argv,
                returncode=returncode,
                stdout=stdout_capture.value,
                stderr=stderr_capture.value,
                stdout_truncated=stdout_capture.truncated,
                stderr_truncated=stderr_capture.truncated,
                elapsed_seconds=time.monotonic() - started,
            )
        finally:
            # A persistent replacement while the subprocess ran prevents any
            # result from being accepted or published.
            self._revalidate_launch_identity(tool)

    def run_checked(
        self,
        tool: ToolName,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        require_complete_stdout: bool = False,
        require_complete_stderr: bool = False,
    ) -> ToolRunResult:
        result = self.run(
            tool,
            arguments,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
        )
        if result.returncode != 0:
            raise io_error("MEDIA_TOOL_FAILED", "media tool reported a processing failure")
        if (require_complete_stdout and result.stdout_truncated) or (
            require_complete_stderr and result.stderr_truncated
        ):
            raise io_error("MEDIA_TOOL_OUTPUT_LIMIT", "media tool output exceeded its safe limit")
        return result

    def discover_identities(
        self,
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> tuple[ToolIdentity, ToolIdentity]:
        initial_snapshots = self._snapshots()
        ffmpeg_version = self._identity_version(
            "ffmpeg", timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes
        )
        ffprobe_version = self._identity_version(
            "ffprobe", timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes
        )
        encoder_result = self.run_checked(
            "ffmpeg",
            ["-hide_banner", "-encoders"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit_bytes,
            stderr_limit_bytes=output_limit_bytes,
            require_complete_stdout=True,
        )
        filter_result = self.run_checked(
            "ffmpeg",
            ["-hide_banner", "-filters"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit_bytes,
            stderr_limit_bytes=output_limit_bytes,
            require_complete_stdout=True,
        )
        protocol_result = self.run_checked(
            "ffmpeg",
            ["-hide_banner", "-protocols"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit_bytes,
            stderr_limit_bytes=output_limit_bytes,
            require_complete_stdout=True,
        )
        muxer_result = self.run_checked(
            "ffmpeg",
            ["-hide_banner", "-muxers"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit_bytes,
            stderr_limit_bytes=output_limit_bytes,
            require_complete_stdout=True,
        )
        ffmpeg_full_help = self.run_checked(
            "ffmpeg",
            ["-hide_banner", "-h", "full"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit_bytes,
            stderr_limit_bytes=output_limit_bytes,
            require_complete_stdout=True,
            require_complete_stderr=True,
        )
        ffprobe_full_help = self.run_checked(
            "ffprobe",
            ["-hide_banner", "-h", "full"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit_bytes,
            stderr_limit_bytes=output_limit_bytes,
            require_complete_stdout=True,
            require_complete_stderr=True,
        )
        required_encoders = ("libx264", "pcm_s16le", "png", "webvtt")
        required_filters = ("format", "pad", "scale", "select", "showinfo")
        required_protocols = ("file",)
        required_muxers = ("image2", "mp4", "wav", "webvtt")
        observed_encoders = _listing_names(encoder_result.stdout)
        observed_filters = _listing_names(filter_result.stdout)
        observed_protocols = _listing_names(protocol_result.stdout, single_column=True)
        observed_muxers = _listing_names(muxer_result.stdout)
        missing = [
            f"{category}:{name}"
            for category, name, observed in (
                *(("encoder", name, observed_encoders) for name in required_encoders),
                *(("filter", name, observed_filters) for name in required_filters),
                *(("protocol", name, observed_protocols) for name in required_protocols),
                *(("muxer", name, observed_muxers) for name in required_muxers),
            )
            if name not in observed
        ]
        if missing:
            raise configuration_error(
                "MEDIA_TOOL_CAPABILITY_UNAVAILABLE",
                "required FFmpeg media capability is unavailable",
            )
        if not all(
            _has_required_decode_limits(result.stdout + b"\n" + result.stderr)
            for result in (ffmpeg_full_help, ffprobe_full_help)
        ):
            raise configuration_error(
                "MEDIA_TOOL_CAPABILITY_UNAVAILABLE",
                "required media decoder limits are unavailable",
            )
        final_snapshots = self._snapshots()
        if final_snapshots != initial_snapshots:
            raise configuration_error(
                "MEDIA_TOOL_IDENTITY_CHANGED", "media tool changed during capability discovery"
            )
        self._pinned_snapshots = final_snapshots
        ffmpeg = ToolIdentity(
            name="ffmpeg",
            version=ffmpeg_version,
            executable_sha256=final_snapshots["ffmpeg"].sha256,
            capabilities=sorted(
                [
                    *(f"encoder:{name}" for name in required_encoders),
                    *(f"filter:{name}" for name in required_filters),
                    *(f"protocol:{name}" for name in required_protocols),
                    *(f"muxer:{name}" for name in required_muxers),
                    "option:max_pixels",
                    "option:max_samples",
                ]
            ),
        )
        ffprobe = ToolIdentity(
            name="ffprobe",
            version=ffprobe_version,
            executable_sha256=final_snapshots["ffprobe"].sha256,
            capabilities=[
                "option:max_pixels",
                "option:max_samples",
                "output:json",
                "probe:show_format",
                "probe:show_streams",
            ],
        )
        return ffmpeg, ffprobe

    def _identity_version(
        self,
        tool: ToolName,
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> str:
        result = self.run_checked(
            tool,
            ["-hide_banner", "-version"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=output_limit_bytes,
            stderr_limit_bytes=output_limit_bytes,
            require_complete_stdout=True,
        )
        first_line = result.stdout.decode("utf-8", errors="replace").splitlines()
        if not first_line:
            raise io_error("MEDIA_TOOL_IDENTITY_INVALID", "media tool identity is unavailable")
        version = " ".join(first_line[0].split())
        if not version.startswith(tool) or len(version) > 256:
            raise io_error("MEDIA_TOOL_IDENTITY_INVALID", "media tool identity is invalid")
        return version


def safe_tool_error(error: Exception) -> VepError:
    """Map unexpected adapter errors without retaining paths or tool output."""
    if isinstance(error, VepError):
        return error
    return io_error("MEDIA_TOOL_FAILED", "media tool processing failed")
