from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes

import pytest

from scripts.benchmark_setup_poc import measure


@pytest.mark.skipif(os.name != "nt", reason="Windows collector")
def test_benchmark_timeout_terminates_only_owned_fixture_tree(tmp_path):
    pid_file = tmp_path / "child.json"
    code = (
        "import json,subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(30)']); "
        f"Path({str(pid_file)!r}).write_text(json.dumps(child.pid)); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        measure([sys.executable, "-I", "-S", "-c", code], timeout=1)
    pid = json.loads(pid_file.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if handle:
        try:
            exit_code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            assert exit_code.value != 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
