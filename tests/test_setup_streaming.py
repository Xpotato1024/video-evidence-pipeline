"""Opt-in real engine streaming; no downloads or production roots."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_native_setup import ENGINE, PAYLOAD, apply, input_for, invoke, launch

pytestmark = pytest.mark.skipif(not ENGINE or not PAYLOAD, reason="native fixture not supplied")


@pytest.mark.parametrize("request_id", ["", "x" * 129])
def test_invalid_request_id_is_bounded_and_does_not_mutate(tmp_path, request_id):
    request = {"schema": "vep.setup.v1", "id": request_id, "op": "capabilities"}
    result = subprocess.run(
        [ENGINE, "--sandbox-root", str(tmp_path), "--protocol=jsonl"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 2 and len(result.stdout) < 1024
    assert json.loads(result.stdout)["id"] is None
    assert json.loads(result.stdout)["code"] == "INVALID_REQUEST"
    assert not list(tmp_path.iterdir())


def streaming(
    root, fields, *, cancel_phase=None, disconnect=False, environment=None, before_cancel=None
):
    _, plan = invoke(root, "plan", **fields)
    request = {
        "schema": "vep.setup.v1",
        "id": "stream",
        "op": "apply",
        "events": True,
        "plan_id": plan["value"]["plan_id"],
        **fields,
    }
    process = subprocess.Popen(
        [ENGINE, "--sandbox-root", str(root), "--protocol=jsonl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    lines = queue.Queue(maxsize=512)

    def collect():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    events = []
    sent = False
    try:
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        while (line := lines.get(timeout=120)) is not None:
            event = json.loads(line)
            events.append(event)
            if event.get("phase") == cancel_phase and not sent:
                if before_cancel:
                    before_cancel()
                if disconnect:
                    process.stdin.close()
                else:
                    process.stdin.write('{"schema":"vep.setup.v1","id":"stream","op":"cancel"}\n')
                    process.stdin.flush()
                sent = True
        code = process.wait(timeout=10)
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
        assert all(event["id"] == "stream" for event in events)
        assert events[-1]["event"] == "result"
        assert sum(event["event"] == "result" for event in events) == 1
        if cancel_phase:
            assert sent
        return code, events
    finally:
        if process.poll() is None:
            process.stdin.close()  # ask the common engine to cancel, never kill during commit
            process.wait(timeout=90)
        reader.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_stream_cancel_checkpoints_keep_active_and_data_and_can_retry(tmp_path):
    root = tmp_path / "stream-sandbox"
    root.mkdir()
    invoke(root, "init-sandbox")
    apply(root, input_for(tmp_path, "baseline"))
    sentinel = root / "data/retained"
    sentinel.write_bytes(b"synthetic retained data\n")
    active = (root / "state/active.json").read_bytes()
    for index, phase in enumerate(("STAGE", "VERIFY", "INVENTORY", "CHECK")):
        fields = input_for(tmp_path, f"cancel-{index}")
        code, events = streaming(root, fields, cancel_phase=phase)
        assert code == 3
        assert events[-1]["status"] == "cancelled"
        assert events[-1]["code"] == "CANCELLED"
        assert (root / "state/active.json").read_bytes() == active
        assert sentinel.read_bytes() == b"synthetic retained data\n"
        invoke(root, "repair")
    # A cancellation is not an uninstall: the same candidate can be replanned.
    apply(root, fields)
    launch(root, "doctor", "--json")


def test_cancel_after_commit_is_deferred_and_final_result_is_authoritative(tmp_path):
    root = tmp_path / "commit-race"
    root.mkdir()
    invoke(root, "init-sandbox")
    fields = input_for(tmp_path, "committed")
    environment = dict(os.environ, VEP_SANDBOX_TEST_COMMIT_DELAY_MS="400")
    code, events = streaming(root, fields, cancel_phase="ACTIVATE", environment=environment)
    assert code == 0
    assert events[-1]["status"] == "ok"
    assert events[-1]["value"]["active"]["generation"] == 1
    acknowledgements = [event for event in events if event["event"] == "cancel_ack"]
    assert acknowledgements and acknowledgements[0]["accepted"] is False
    assert acknowledgements[0]["final"] is False
    launch(root, "doctor", "--json")


def test_streaming_disconnect_cancels_but_legacy_eof_still_executes(tmp_path):
    root = tmp_path / "disconnect"
    root.mkdir()
    invoke(root, "init-sandbox")
    fields = input_for(tmp_path, "disconnect-candidate")
    code, events = streaming(root, fields, cancel_phase="STAGE", disconnect=True)
    assert code == 3 and events[-1]["status"] == "cancelled"
    assert json.loads((root / "state/active.json").read_bytes())["generation"] == 0
    # invoke uses subprocess.run(input=...), which closes stdin after one request.
    _, legacy = apply(root, fields)
    assert legacy["status"] == "ok" and legacy["seq"] == 1


def test_stalled_output_consumer_does_not_hold_activation_locks(tmp_path):
    root = tmp_path / "backpressure"
    root.mkdir()
    invoke(root, "init-sandbox")
    fields = input_for(tmp_path, "backpressure")
    _, plan = invoke(root, "plan", **fields)
    process = subprocess.Popen(
        [ENGINE, "--sandbox-root", str(root), "--protocol=jsonl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        request = {
            "schema": "vep.setup.v1",
            "id": "blocked-reader",
            "op": "apply",
            "events": True,
            "plan_id": plan["value"]["plan_id"],
            **fields,
        }
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        # Deliberately do not read stdout until a separate status request sees commit.
        deadline = time.monotonic() + 60
        observed = None
        while time.monotonic() < deadline:
            _, status = invoke(root, "status", ok=False)
            if status.get("value", {}).get("active", {}).get("generation") == 1:
                observed = status
                break
            time.sleep(0.05)
        assert observed and observed["value"]["recovery_required"] is False
        output, error = process.communicate(timeout=15)
        events = [json.loads(line) for line in output.splitlines()]
        assert events[-1]["status"] == "ok" and process.returncode == 0, error
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    finally:
        if process.poll() is None:
            process.stdin.close()
            process.stdin = None
            process.communicate(timeout=90)


@pytest.mark.skipif(os.name != "nt", reason="Windows candidate job containment")
def test_candidate_cancel_stops_owned_child_and_grandchild(tmp_path):
    import ctypes

    root = tmp_path / "candidate-tree"
    root.mkdir()
    invoke(root, "init-sandbox")
    payload = tmp_path / "synthetic-payload"
    shutil.copytree(PAYLOAD, payload)
    bootstrap = payload / "payload/bootstrap.py"
    bootstrap.write_text(
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "context=json.loads(Path(sys.argv[1]).read_text())\n"
        "child=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(60)'])\n"
        "Path(context['data_root'],'candidate-pids.json').write_text(json.dumps([os.getpid(),child.pid]))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fields = input_for(tmp_path, "candidate-tree")
    manifest_path = Path(fields["manifest"])
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest["files"]:
        if entry["path"] == "payload/bootstrap.py":
            entry.update(
                size=bootstrap.stat().st_size,
                sha256=hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
            )
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path.write_bytes(encoded)
    fields.update(
        payload=str(payload.resolve()), manifest_sha256=hashlib.sha256(encoded).hexdigest()
    )
    handles = []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]

    def observe_owned_tree():
        path = root / "data/candidate-pids.json"
        deadline = time.monotonic() + 15
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pids = json.loads(path.read_bytes())
        assert len(pids) == 2
        for pid in pids:
            handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
            assert handle
            handles.append(handle)
            assert kernel.WaitForSingleObject(handle, 0) == 258  # still alive

    try:
        code, events = streaming(
            root, fields, cancel_phase="CHECK", before_cancel=observe_owned_tree
        )
        assert code == 3 and events[-1]["code"] == "CANCELLED"
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
        assert len(handles) == 2
        _, status = invoke(root, "status")
        assert status["value"]["active"]["generation"] == 0
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
