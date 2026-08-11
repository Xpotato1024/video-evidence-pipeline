from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from video_evidence_pipeline.acquisition.process_tree import ManagedProcessTree


def test_managed_process_tree_stops_spawned_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "synthetic-heartbeat"
    child = (
        "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);\n"
        "while True:\n"
        " p.open('ab').write(b'x'); time.sleep(0.02)"
    )
    parent = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
        "time.sleep(60)"
    )
    tree = ManagedProcessTree.start(
        [sys.executable, "-c", parent, child, str(marker)],
        process_factory=subprocess.Popen,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        for _ in range(150):
            if marker.exists() and marker.stat().st_size >= 2:
                break
            assert tree.poll() is None
            time.sleep(0.02)
        else:
            raise AssertionError("synthetic descendant did not start")

        tree.stop()
        time.sleep(0.2)
        stopped_size = marker.stat().st_size
        time.sleep(0.3)
        assert marker.stat().st_size == stopped_size
    finally:
        tree.stop()


def test_managed_process_tree_closes_orphan_after_parent_success(tmp_path: Path) -> None:
    marker = tmp_path / "synthetic-orphan-heartbeat"
    child = (
        "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);\n"
        "while True:\n"
        " p.open('ab').write(b'x'); time.sleep(0.02)"
    )
    parent = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
        "time.sleep(0.1)"
    )
    tree = ManagedProcessTree.start(
        [sys.executable, "-c", parent, child, str(marker)],
        process_factory=subprocess.Popen,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        for _ in range(150):
            if tree.poll() is not None and marker.exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("synthetic parent did not exit")

        tree.close()
        time.sleep(0.2)
        stopped_size = marker.stat().st_size
        time.sleep(0.3)
        assert marker.stat().st_size == stopped_size
    finally:
        tree.stop()
