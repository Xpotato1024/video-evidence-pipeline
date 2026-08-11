from __future__ import annotations

import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class _WindowsJob:
    def __init__(self, process: ProcessLike) -> None:
        import ctypes
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = getattr(process, "_handle", None)
            if process_handle is None or not kernel32.AssignProcessToJobObject(
                handle, wintypes.HANDLE(process_handle)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            kernel32.CloseHandle(handle)
            raise
        self._kernel32 = kernel32
        self._handle = handle

    def terminate(self) -> None:
        import ctypes

        if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class _WindowsLaunchGate:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        name = f"Local\\VEP-ytdlp-{uuid.uuid4().hex}"
        handle = kernel32.CreateEventW(None, True, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.name = name
        self._kernel32 = kernel32
        self._handle = handle

    def release(self) -> None:
        import ctypes

        if self._handle is None or not self._kernel32.SetEvent(self._handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class ManagedProcessTree:
    """Own a subprocess and every descendant it creates."""

    def __init__(
        self,
        process: ProcessLike,
        *,
        production_tree: bool,
        windows_job: _WindowsJob | None,
        windows_gate: _WindowsLaunchGate | None,
    ) -> None:
        self._process = process
        self._production_tree = production_tree
        self._windows_job = windows_job
        self._windows_gate = windows_gate
        self._closed = False

    @classmethod
    def start(
        cls,
        argv: list[str],
        *,
        process_factory: Callable[..., ProcessLike],
        **kwargs: object,
    ) -> ManagedProcessTree:
        production_tree = process_factory is subprocess.Popen
        launch_kwargs = dict(kwargs)
        launch_argv = argv
        windows_gate = None
        if production_tree:
            if os.name == "nt":
                windows_gate = _WindowsLaunchGate()
                launch_argv = [
                    sys.executable,
                    "-m",
                    "video_evidence_pipeline.acquisition.windows_job_launcher",
                    windows_gate.name,
                    *argv,
                ]
                launch_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                launch_kwargs["start_new_session"] = True
        try:
            process = process_factory(launch_argv, **launch_kwargs)
        except Exception:
            if windows_gate is not None:
                windows_gate.close()
            raise
        windows_job = None
        if production_tree and os.name == "nt":
            try:
                windows_job = _WindowsJob(process)
                assert windows_gate is not None
                windows_gate.release()
            except Exception:
                with suppress(OSError):
                    process.kill()
                with suppress(OSError, subprocess.TimeoutExpired):
                    process.wait(timeout=1)
                if windows_job is not None:
                    windows_job.close()
                windows_gate.close()
                raise
        return cls(
            process,
            production_tree=production_tree,
            windows_job=windows_job,
            windows_gate=windows_gate,
        )

    def poll(self) -> int | None:
        return self._process.poll()

    def stop(self) -> None:
        if self._closed:
            return
        if self._production_tree and os.name != "nt":
            with suppress(OSError):
                os.killpg(self._process.pid, signal.SIGTERM)
        elif self._windows_job is not None:
            with suppress(OSError):
                self._windows_job.terminate()
        else:
            with suppress(OSError):
                self._process.terminate()
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if self._production_tree and os.name != "nt":
                with suppress(OSError):
                    os.killpg(self._process.pid, signal.SIGKILL)
            elif self._windows_job is not None:
                with suppress(OSError):
                    self._windows_job.terminate()
            else:
                with suppress(OSError):
                    self._process.kill()
            with suppress(OSError, subprocess.TimeoutExpired):
                self._process.wait(timeout=1)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._windows_job is not None:
            self._windows_job.close()
            self._windows_job = None
        if self._windows_gate is not None:
            self._windows_gate.close()
            self._windows_gate = None
        self._closed = True
