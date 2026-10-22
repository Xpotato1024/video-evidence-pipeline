"""Serial, offline pHash comparison on generated RGB PNGs; no user media.

Run with the checkout's Python. --reference is a saved perceptual.py from the
baseline revision. Each timed sample uses a fresh process; fixture generation
and imports are excluded from elapsed time. RSS includes interpreter/imports.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path


def synthetic_png(width: int, height: int, *, color_type: int = 2, filter_type: int = 0) -> bytes:
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    previous = bytes(width * channels)
    for row in range(height):
        current = bytes(
            (column * 17 + row * 31 + (column // channels) ** 2) % 256
            for column in range(width * channels)
        )
        rows.append(filter_type)
        for index, value in enumerate(current):
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            corner = previous[index - channels] if index >= channels else 0
            estimate = left + above - corner
            distances = [abs(estimate - item) for item in (left, above, corner)]
            paeth = (left, above, corner)[distances.index(min(distances))]
            predictor = (0, left, above, (left + above) // 2, paeth)[filter_type]
            rows.append((value - predictor) & 255)
        previous = current
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def peak_rss_bytes() -> int:
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
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
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def worker(module_path: Path, fixture: Path, pixels: int) -> None:
    spec = importlib.util.spec_from_file_location("measured_perceptual", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    started = time.perf_counter()
    digest = module.perceptual_hash_png(
        fixture, maximum_bytes=16 * 1024 * 1024, maximum_pixels=pixels
    )
    elapsed = time.perf_counter() - started
    rss = peak_rss_bytes()
    print(json.dumps({"seconds": elapsed, "peak_rss_bytes": rss, "phash": digest}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--contention-note", default="uncontrolled shared host; other tasks may be active"
    )
    parser.add_argument("--runs", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--worker", nargs=3, metavar=("MODULE", "PNG", "PIXELS"))
    args = parser.parse_args()
    if args.worker:
        worker(Path(args.worker[0]), Path(args.worker[1]), int(args.worker[2]))
        return
    if args.reference is None or args.output is None:
        parser.error("--reference and --output are required")
    current = (
        Path(__file__).resolve().parents[1] / "src/video_evidence_pipeline/media/perceptual.py"
    )
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "contention_note": args.contention_note,
        "scope": "pHash only; serial fresh workers; elapsed excludes imports; RSS is process lifetime peak including interpreter/imports; no allocation tracing overhead",
        "modules": {
            label: hashlib.sha256(path.read_bytes()).hexdigest()
            for label, path in (("reference", args.reference), ("current", current))
        },
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="vep-benchmark-") as temporary:
        for width, height, filtering in ((320, 180, 0), (320, 180, 4), (1920, 1080, 0)):
            payload = synthetic_png(width, height, filter_type=filtering)
            fixture = Path(temporary) / "synthetic.png"
            fixture.write_bytes(payload)
            case = {
                "width": width,
                "height": height,
                "filter": filtering,
                "fixture_sha256": hashlib.sha256(payload).hexdigest(),
                "reference": [],
                "current": [],
            }
            for repeat in range(args.runs):
                order = (("reference", args.reference), ("current", current))
                for label, module in order if repeat % 2 == 0 else reversed(order):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--worker",
                            str(module.resolve()),
                            str(fixture),
                            str(width * height),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=60,
                    )
                    case[label].append(json.loads(result.stdout))
            if (
                len(
                    {
                        sample["phash"]
                        for label in ("reference", "current")
                        for sample in case[label]
                    }
                )
                != 1
            ):
                raise RuntimeError("pHash output differs; do not report a performance improvement")
            case["median_seconds"] = {
                label: statistics.median(sample["seconds"] for sample in case[label])
                for label in ("reference", "current")
            }
            report["cases"].append(case)
            print(
                json.dumps(
                    {
                        "width": width,
                        "height": height,
                        "filter": filtering,
                        "median_seconds": case["median_seconds"],
                    }
                ),
                flush=True,
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
