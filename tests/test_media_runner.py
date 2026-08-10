from __future__ import annotations

import io
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from video_evidence_pipeline.domain.hashing import sha256_file
from video_evidence_pipeline.domain.media import ToolIdentity
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.media.runner import FFmpegRunner, _has_required_decode_limits


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"ok",
        stderr: bytes = b"",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class BrokenStream:
    def read(self, _size: int = -1) -> bytes:
        raise RuntimeError("synthetic pipe failure")


def runner_with_process(
    process: FakeProcess,
    observed: dict[str, object],
    tmp_path: Path,
    on_launch: Callable[[], None] | None = None,
) -> FFmpegRunner:
    def factory(argv: list[str], **kwargs: object) -> FakeProcess:
        observed["argv"] = argv
        observed.update(kwargs)
        if on_launch is not None:
            on_launch()
        return process

    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_bytes(b"synthetic ffmpeg executable")
    ffprobe.write_bytes(b"synthetic ffprobe executable")
    return FFmpegRunner(
        ffmpeg_executable=ffmpeg,
        ffprobe_executable=ffprobe,
        popen_factory=factory,
    )


def _pinned_identities(tmp_path: Path) -> tuple[ToolIdentity, ToolIdentity]:
    return (
        ToolIdentity(
            name="ffmpeg",
            version="ffmpeg synthetic",
            executable_sha256=sha256_file(tmp_path / "ffmpeg"),
            capabilities=[],
        ),
        ToolIdentity(
            name="ffprobe",
            version="ffprobe synthetic",
            executable_sha256=sha256_file(tmp_path / "ffprobe"),
            capabilities=[],
        ),
    )


def test_runner_uses_fixed_argv_no_shell_and_disabled_stdin(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    runner = runner_with_process(FakeProcess(), observed, tmp_path)

    result = runner.run(
        "ffprobe",
        ["-v", "error", "synthetic.mp4"],
        timeout_seconds=1,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
    )

    assert Path(str(observed["argv"][0])).name == "ffprobe"
    assert observed["argv"][1:] == ["-v", "error", "synthetic.mp4"]
    assert observed["shell"] is False
    assert observed["stdin"] == subprocess.DEVNULL
    assert observed["stdout"] == subprocess.PIPE
    assert observed["stderr"] == subprocess.PIPE
    assert result.stdout == b"ok"


def test_runner_bounds_both_output_streams_while_draining(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    runner = runner_with_process(
        FakeProcess(stdout=b"x" * 100, stderr=b"y" * 100), observed, tmp_path
    )

    result = runner.run(
        "ffmpeg",
        ["-version"],
        timeout_seconds=1,
        stdout_limit_bytes=10,
        stderr_limit_bytes=11,
    )

    assert result.stdout == b"x" * 10
    assert result.stderr == b"y" * 11
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_runner_maps_capture_thread_failure_to_safe_error(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    process = FakeProcess()
    process.stdout = BrokenStream()  # type: ignore[assignment]
    runner = runner_with_process(process, observed, tmp_path)

    with pytest.raises(VepError) as captured:
        runner.run(
            "ffmpeg",
            ["-version"],
            timeout_seconds=1,
            stdout_limit_bytes=100,
            stderr_limit_bytes=100,
        )

    assert captured.value.code == "MEDIA_TOOL_PIPE_FAILED"
    assert "synthetic pipe failure" not in repr(captured.value)


def test_runner_kills_process_on_wallclock_timeout(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    process = FakeProcess(timeout=True)
    runner = runner_with_process(process, observed, tmp_path)

    with pytest.raises(VepError) as captured:
        runner.run(
            "ffmpeg",
            ["-version"],
            timeout_seconds=0.01,
            stdout_limit_bytes=100,
            stderr_limit_bytes=100,
        )

    assert captured.value.code == "MEDIA_TOOL_TIMEOUT"
    assert process.killed is True


def test_runner_maps_nonzero_exit_without_exposing_tool_output(tmp_path: Path) -> None:
    secret = "synthetic-private-path"
    observed: dict[str, object] = {}
    runner = runner_with_process(
        FakeProcess(stderr=f"failure at {secret}".encode(), returncode=1), observed, tmp_path
    )

    with pytest.raises(VepError) as captured:
        runner.run_checked(
            "ffmpeg",
            ["-version"],
            timeout_seconds=1,
            stdout_limit_bytes=100,
            stderr_limit_bytes=100,
        )

    assert captured.value.code == "MEDIA_TOOL_FAILED"
    assert secret not in repr(captured.value)


def test_runner_rejects_nul_arguments_before_start(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    runner = runner_with_process(FakeProcess(), observed, tmp_path)

    with pytest.raises(VepError) as captured:
        runner.run(
            "ffmpeg",
            ["bad\x00argument"],
            timeout_seconds=1,
            stdout_limit_bytes=100,
            stderr_limit_bytes=100,
        )

    assert captured.value.code == "MEDIA_TOOL_ARGUMENT_INVALID"
    assert "argv" not in observed


def test_runner_rejects_executable_replacement_after_pinning(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    runner = runner_with_process(FakeProcess(), observed, tmp_path)
    ffmpeg_path = tmp_path / "ffmpeg"
    ffmpeg, ffprobe = _pinned_identities(tmp_path)

    runner.pin_identities(ffmpeg, ffprobe)
    ffmpeg_path.write_bytes(b"replacement executable")

    with pytest.raises(VepError) as captured:
        runner.revalidate_pinned_identities(ffmpeg, ffprobe)

    assert captured.value.code == "MEDIA_TOOL_IDENTITY_CHANGED"


def test_runner_rejects_persistent_replacement_before_a_pinned_launch(tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    runner = runner_with_process(FakeProcess(), observed, tmp_path)
    ffmpeg, ffprobe = _pinned_identities(tmp_path)
    runner.pin_identities(ffmpeg, ffprobe)
    (tmp_path / "ffmpeg").write_bytes(b"persistent replacement")

    with pytest.raises(VepError) as captured:
        runner.run(
            "ffmpeg",
            ["-version"],
            timeout_seconds=1,
            stdout_limit_bytes=100,
            stderr_limit_bytes=100,
        )

    assert captured.value.code == "MEDIA_TOOL_IDENTITY_CHANGED"
    assert "argv" not in observed


def test_runner_rejects_persistent_replacement_after_a_pinned_launch(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def replace_after_launch() -> None:
        (tmp_path / "ffmpeg").write_bytes(b"persistent replacement")

    runner = runner_with_process(FakeProcess(), observed, tmp_path, on_launch=replace_after_launch)
    ffmpeg, ffprobe = _pinned_identities(tmp_path)
    runner.pin_identities(ffmpeg, ffprobe)

    with pytest.raises(VepError) as captured:
        runner.run(
            "ffmpeg",
            ["-version"],
            timeout_seconds=1,
            stdout_limit_bytes=100,
            stderr_limit_bytes=100,
        )

    assert captured.value.code == "MEDIA_TOOL_IDENTITY_CHANGED"
    assert Path(str(observed["argv"][0])).name == "ffmpeg"


def test_generic_decode_limit_capability_requires_int64_global_options() -> None:
    assert _has_required_decode_limits(
        b"""
        -max_pixels <int64> ED.VAS Maximum number of pixels (from 0 to INT_MAX)
        -max_samples <int64> ED..A Maximum number of samples (from 0 to INT_MAX)
        """
    )
    assert not _has_required_decode_limits(
        b"""
        -max_pixels <int> ED.VAS Maximum number of pixels (from 0 to INT_MAX)
        -max_samples <int> ED..A Maximum number of samples decoded per call
        """
    )
