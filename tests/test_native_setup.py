"""Opt-in real native/private-runtime tests; no network or production installation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ENGINE = os.environ.get("VEP_NATIVE_SETUP_BIN")
PAYLOAD = os.environ.get("VEP_PRIVATE_PAYLOAD")
pytestmark = pytest.mark.skipif(
    not ENGINE or not PAYLOAD, reason="native development binary/payload not supplied"
)


def invoke(root: Path, op: str, *, ok=True, environment=None, **fields):
    request = {"schema": "vep.setup.v1", "id": "fixture", "op": op, **fields}
    result = subprocess.run(
        [ENGINE, "--sandbox-root", str(root), "--protocol=jsonl"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=environment,
    )
    if ok:
        assert result.returncode == 0, (result.stdout, result.stderr)
    value = json.loads(result.stdout) if result.stdout else {}
    if result.returncode != 86:
        assert value["id"] == "fixture"
    return result, value


def input_for(tmp_path: Path, release: str):
    original_root = Path(PAYLOAD).resolve()
    original = original_root.parent / f"{original_root.name}.manifest.json"
    data = json.loads(original.read_bytes())
    data["release_id"] = release
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    manifest = tmp_path / f"{release}.manifest.json"
    manifest.write_bytes(encoded)
    return {
        "manifest": str(manifest),
        "payload": str(Path(PAYLOAD).resolve()),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def apply(root: Path, fields: dict, **kwargs):
    _, plan = invoke(root, "plan", **fields)
    return invoke(root, "apply", plan_id=plan["value"]["plan_id"], **fields, **kwargs)


def launch(root: Path, *args, release=None, ok=True):
    command = [ENGINE, "--sandbox-root", str(root), "run"]
    if release:
        command.extend(["--release", release])
    result = subprocess.run(
        [*command, "--", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    if ok:
        assert result.returncode == 0, (result.stdout, result.stderr)
    return result


def file_bytes(root: Path):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_native_install_update_rollback_and_retained_job_versions(tmp_path):
    root = tmp_path / "sandbox 日本語"
    root.mkdir()
    invoke(root, "init-sandbox")
    first = input_for(tmp_path, "release-one")
    second = input_for(tmp_path, "release-two")
    before = file_bytes(root)
    invoke(root, "plan", **first)
    assert file_bytes(root) == before  # planning performs no filesystem writes
    _, installed = apply(root, first)
    assert installed["value"]["active"]["generation"] == 1
    assert (
        json.loads(launch(root, "doctor", "--json").stdout)["installation_kind"]
        == "private-runtime"
    )
    source = tmp_path / "synthetic-notes.txt"
    source.write_text("synthetic transcript\n", encoding="utf-8")
    old_job = json.loads(
        launch(
            root,
            "job",
            "init",
            "old-job",
            "--source-kind",
            "local_file",
            "--source",
            source,
            "--primary-input",
            "transcript",
            "--task-type",
            "summary",
            "--language",
            "ja",
            "--output-format",
            "markdown",
            "--json",
        ).stdout
    )["workspace"]
    old_path = Path(old_job)
    old_bytes = file_bytes(old_path)
    apply(root, second)
    mismatch = launch(root, "job", "status", old_path, "--json", ok=False)
    assert mismatch.returncode != 0
    assert json.loads(mismatch.stdout)["error"]["code"] == "JOB_RUNTIME_MISMATCH"
    assert (
        json.loads(launch(root, "job", "status", old_path, "--json", release="release-one").stdout)[
            "stored_state"
        ]
        == "CREATED"
    )
    new_job = json.loads(
        launch(
            root,
            "job",
            "init",
            "new-job",
            "--source-kind",
            "local_file",
            "--source",
            source,
            "--primary-input",
            "transcript",
            "--task-type",
            "summary",
            "--language",
            "ja",
            "--output-format",
            "markdown",
            "--json",
        ).stdout
    )["workspace"]
    new_bytes = file_bytes(Path(new_job))
    _, plan = invoke(root, "plan", **first)
    invoke(root, "rollback", release_id="release-one", plan_id=plan["value"]["plan_id"])
    assert launch(root, "job", "status", new_job, "--json", ok=False).returncode != 0
    launch(root, "job", "status", new_job, "--json", release="release-two")
    assert file_bytes(old_path) == old_bytes
    assert file_bytes(Path(new_job)) == new_bytes
    assert (root / "app/versions/release-one").is_dir()
    assert (root / "app/versions/release-two").is_dir()
    assert not list((root / "app").rglob("__pycache__"))
    # Corrupt executable/resource bytes must fail before worker execution.
    target = root / "app/versions/release-one/payload/bootstrap.py"
    target.write_bytes(target.read_bytes() + b"\n# corrupted synthetic fixture\n")
    corrupted = launch(root, "--version", ok=False)
    assert json.loads(corrupted.stdout)["code"] == "PAYLOAD_HASH_MISMATCH"


def test_native_interruption_repair_stale_plan_and_no_data_deletion(tmp_path):
    root = tmp_path / "interruption"
    root.mkdir()
    invoke(root, "init-sandbox")
    fields = input_for(tmp_path, "retained")
    apply(root, fields)
    sentinel = root / "data/sentinel"
    sentinel.write_bytes(b"synthetic retained data\n")
    for point in ("prepared", "after-replace", "committed"):
        _, plan = invoke(root, "plan", **fields)
        environment = dict(os.environ, VEP_SANDBOX_INTERRUPT=point)
        crashed, _ = invoke(
            root,
            "apply",
            ok=False,
            environment=environment,
            plan_id=plan["value"]["plan_id"],
            **fields,
        )
        assert crashed.returncode == 86
        invoke(root, "repair")
        launch(root, "doctor", "--json")
        assert sentinel.read_bytes() == b"synthetic retained data\n"
    _, old_plan = invoke(root, "plan", **fields)
    apply(root, fields)
    _, failed = invoke(root, "apply", ok=False, plan_id=old_plan["value"]["plan_id"], **fields)
    assert failed["code"] == "PLAN_STALE"
    active_before = (root / "state/active.json").read_bytes()
    # Damage makes state uncertain: repair must stop, retaining every file.
    (root / "state/active.json").write_bytes(b"{truncated")
    before = file_bytes(root)
    _, repair = invoke(root, "repair", ok=False)
    assert repair["code"] == "STATE_INTEGRITY_FAILED"
    assert file_bytes(root) == before
    (root / "state/active.json").write_bytes(active_before)


def test_native_rejects_bad_metadata_before_mutation_and_writer_contention(tmp_path):
    root = tmp_path / "bad-input"
    root.mkdir()
    invoke(root, "init-sandbox")
    fields = input_for(tmp_path, "invalid-input")
    before = file_bytes(root)
    _, invalid = invoke(root, "plan", ok=False, **{**fields, "manifest_sha256": "0" * 64})
    assert invalid["code"] == "MANIFEST_HASH_MISMATCH"
    manifest_path = Path(fields["manifest"])
    original = manifest_path.read_bytes()
    for path in (
        "../escape",
        "C:/escape",
        "payload/CON.txt",
        "payload/trailing.",
        "payload/x:stream",
    ):
        data = json.loads(original)
        data["files"][0]["path"] = path
        encoded = json.dumps(data).encode()
        manifest_path.write_bytes(encoded)
        _, invalid = invoke(
            root,
            "plan",
            ok=False,
            **{**fields, "manifest_sha256": hashlib.sha256(encoded).hexdigest()},
        )
        assert invalid["code"] == "MANIFEST_INVALID"
    assert file_bytes(root) == before
    manifest_path.write_bytes(original)
    if os.name == "nt":
        import msvcrt

        with (root / "state/writer.lock").open("r+b") as locked:
            msvcrt.locking(locked.fileno(), msvcrt.LK_NBLCK, 1)
            _, plan = invoke(root, "plan", **fields)
            _, busy = invoke(root, "apply", ok=False, plan_id=plan["value"]["plan_id"], **fields)
            assert busy["code"] == "OPERATION_IN_PROGRESS"
    assert file_bytes(root) == before


def test_native_copy_and_promote_interruption_retries_retain_data(tmp_path):
    root = tmp_path / "early-interruption"
    root.mkdir()
    invoke(root, "init-sandbox")
    apply(root, input_for(tmp_path, "baseline"))
    sentinel = root / "data/sentinel"
    sentinel.write_bytes(b"synthetic retained data\n")
    for index, point in enumerate(("after-copy", "after-promote")):
        fields = input_for(tmp_path, f"candidate-{index}")
        active = (root / "state/active.json").read_bytes()
        _, plan = invoke(root, "plan", **fields)
        crashed, _ = invoke(
            root,
            "apply",
            ok=False,
            environment=dict(os.environ, VEP_SANDBOX_INTERRUPT=point),
            plan_id=plan["value"]["plan_id"],
            **fields,
        )
        assert crashed.returncode == 86
        assert (root / "state/active.json").read_bytes() == active
        retained_stages = file_bytes(root / "app/staging")
        invoke(root, "repair")
        launch(root, "doctor", "--json")
        apply(root, fields)
        launch(root, "doctor", "--json")
        assert sentinel.read_bytes() == b"synthetic retained data\n"
        assert (root / "app/versions/baseline").is_dir()
        after = file_bytes(root / "app/staging")
        assert all(after[name] == content for name, content in retained_stages.items())


def test_jsonl_result_arrives_before_stdin_eof(tmp_path):
    import queue
    import threading

    process = subprocess.Popen(
        [ENGINE, "--sandbox-root", str(tmp_path), "--protocol=jsonl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        process.stdin.write('{"schema":"vep.setup.v1","id":"live","op":"capabilities"}\n')
        process.stdin.flush()
        result = json.loads(lines.get(timeout=5))
        assert result["id"] == "live"
        assert result["value"]["max_requests"] == 1
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        reader.join(timeout=5)
