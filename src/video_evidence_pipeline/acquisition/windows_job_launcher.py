from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes


def _wait_for_parent_gate(name: str) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenEventW(0x00100000, False, name)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0
    finally:
        kernel32.CloseHandle(handle)


def main() -> int:
    if sys.platform != "win32" or len(sys.argv) < 3:
        return 125
    if not _wait_for_parent_gate(sys.argv[1]):
        return 125
    try:
        completed = subprocess.run(
            sys.argv[2:],
            check=False,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return 125
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
