"""Serial Rust/Go comparison on synthetic local data; not a pipeline benchmark."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path


class ProcessMemory(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        *[
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
                "PrivateUsage",
            )
        ],
    ]


def resume_fixture(pid: int) -> None:
    """Resume the one primary thread after assigning the suspended child to a Job."""

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = kernel.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
    ]
    kernel.Thread32First.restype = kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry(dwSize=ctypes.sizeof(ThreadEntry))
        available = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.th32OwnerProcessID == pid:
                thread = kernel.OpenThread(2, False, entry.th32ThreadID)
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel.CloseHandle(thread)
                return
            available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise RuntimeError("suspended fixture thread unavailable")
    finally:
        kernel.CloseHandle(snapshot)


def measure(command: list[str], *, timeout: float = 60) -> dict:
    from video_evidence_pipeline.acquisition.process_tree import _WindowsJob

    start = time.perf_counter_ns()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=4
    )  # CREATE_SUSPENDED
    job = None
    try:
        job = _WindowsJob(process)
        resume_fixture(process.pid)
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        if job is not None:
            job.terminate()
        else:
            process.kill()  # still suspended: it cannot have spawned descendants
        process.communicate(timeout=5)
        raise
    finally:
        if job is not None:
            job.close()
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    if process.returncode:
        raise RuntimeError(f"fixture failed: {stderr[:256]!r}, {stdout[:256]!r}")
    counters = ProcessMemory(cb=ctypes.sizeof(ProcessMemory))
    api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    api.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    api.restype = wintypes.BOOL
    if not api(int(process._handle), ctypes.byref(counters), ctypes.sizeof(counters)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "elapsed_ms": elapsed,
        "parent_peak_rss_bytes": counters.PeakWorkingSetSize,
        "parent_peak_commit_bytes": counters.PeakPagefileUsage,
        "stdout": stdout.decode("utf-8"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=9)
    args = parser.parse_args()
    if os.name != "nt" or not 3 <= args.samples <= 99:
        parser.error("this bounded memory collector requires Windows and 3..99 samples")
    args.work.mkdir(exist_ok=False, parents=True)
    payload = args.work / "payload"
    payload.mkdir()
    entries = []
    for index in range(16):
        data = bytes((i * 17 + index) % 256 for i in range(256 * 1024))
        path = f"part-{index:02}.bin"
        (payload / path).write_bytes(data)
        entries.append(
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    manifest = args.work / "manifest.json"
    manifest.write_text(json.dumps({"schema": "vep.poc.v1", "files": entries}), encoding="utf-8")
    worker = args.work / "worker.py"
    worker.write_text('print("synthetic-worker")\n', encoding="utf-8")
    candidates = {"rust": args.rust.resolve(), "go": args.go.resolve()}
    results: dict[str, dict] = {}
    for operation in ("capabilities", "verify", "extract", "launch"):
        results[operation] = {name: [] for name in candidates}
        for sample in range(args.samples):
            names = list(candidates) if sample % 2 == 0 else list(reversed(candidates))
            for name in names:
                command = [str(candidates[name]), operation]
                if operation in ("verify", "extract"):
                    command.extend([str(manifest), str(payload)])
                if operation == "extract":
                    command.append(str(args.work / f"out-{sample}-{name}"))
                if operation == "launch":
                    command.extend([sys.executable, str(worker)])
                observed = measure(command)
                results[operation][name].append(observed)
        assert {r["stdout"] for rows in results[operation].values() for r in rows}.__len__() == 1
    for output in args.work.glob("out-*"):
        for entry in entries:
            assert (
                hashlib.sha256((output / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
            )
    summary = {
        operation: {
            name: {
                key: statistics.median(row[key] for row in rows)
                for key in (
                    "elapsed_ms",
                    "parent_peak_rss_bytes",
                    "parent_peak_commit_bytes",
                )
            }
            for name, rows in groups.items()
        }
        for operation, groups in results.items()
    }
    result = {
        "schema": "vep.setup-poc-benchmark.v1",
        "samples": args.samples,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
        "fixture": {
            "files": 16,
            "bytes": 4194304,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        },
        "binaries": {
            name: {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for name, path in candidates.items()
        },
        "conditions": [
            "serial alternating fresh processes",
            "uncontrolled warm OS/AV cache",
            "shared host; other tasks not isolated; builds completed before measurement",
            "includes suspended-create/job assignment/thread-resume harness overhead equally",
            "unsigned developer binaries; no product payload or signature validation",
            "RSS/commit are OS parent process lifetime peaks; child memory excluded",
            "launch uses identical system Python -I -S noop; not private-runtime performance",
            "size is executable only, excluding licenses/runtime/resources; not distribution size",
        ],
        "summary": summary,
        "raw": results,
    }
    (args.work / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "binaries": result["binaries"]}, indent=2))


if __name__ == "__main__":
    main()
