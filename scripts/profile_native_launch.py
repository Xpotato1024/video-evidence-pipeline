"""Profile a development sandbox doctor, keeping all verification enabled.

No downloads, production installation, cache changes or real job inputs. Wall
time includes the worker; nested child timings must never be added to it again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import time
from pathlib import Path


def run(command, *, request=None):
    started = time.perf_counter()
    process = subprocess.run(
        list(map(str, command)),
        input=None if request is None else json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    return process, (time.perf_counter() - started) * 1000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    if not 3 <= args.samples <= 15 or args.sandbox.exists() or args.output.exists():
        parser.error("use fresh sandbox/output and 3..15 samples")
    engine = args.engine.resolve(strict=True)
    payload = args.payload.resolve(strict=True)
    manifest = payload.parent / f"{payload.name}.manifest.json"
    fields = {
        "manifest": str(manifest),
        "payload": str(payload),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    args.sandbox.mkdir(parents=True)
    sandbox = args.sandbox.resolve(strict=True)
    common = [engine, "--sandbox-root", sandbox]

    def operation(op, **extra):
        request = {"schema": "vep.setup.v1", "id": "profile", "op": op, **extra}
        process, _ = run([*common, "--protocol=jsonl"], request=request)
        return json.loads(process.stdout)

    operation("init-sandbox")
    plan = operation("plan", **fields)
    operation("apply", plan_id=plan["value"]["plan_id"], **fields)
    rows = {"plain": [], "profile": [], "interpreter_noop": []}
    canonical = None
    for index in range(args.samples):
        modes = ("plain", "profile") if index % 2 == 0 else ("profile", "plain")
        for mode in modes:
            process, elapsed = run(
                [
                    *common,
                    "run-profile" if mode == "profile" else "run",
                    "--",
                    "doctor",
                    "--json",
                ]
            )
            output = json.loads(process.stdout)
            if canonical is None:
                canonical = output
            assert output == canonical, "doctor output changed"
            row = {"wall_ms": elapsed}
            if mode == "profile":
                records = [json.loads(line) for line in process.stderr.splitlines()]
                assert {record["scope"] for record in records} == {
                    "sandbox_open",
                    "native",
                    "worker",
                }
                row["phases"] = records
            else:
                assert process.stderr == ""
            rows[mode].append(row)
        _, elapsed = run(
            [payload / "runtime/python.exe", "-I", "-B", "-S", "-X", "utf8", "-c", "pass"]
        )
        rows["interpreter_noop"].append({"wall_ms": elapsed})
    result = {
        "schema": "vep.launch-costs.v1",
        "samples": args.samples,
        "engine_sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
        "manifest_sha256": fields["manifest_sha256"],
        "fixture": {
            "files": len(json.loads(manifest.read_bytes())["files"]),
            "doctor_outputs_equal": True,
        },
        "conditions": [
            "serial alternating fresh processes, all file/hash/inventory checks retained",
            "shared host, warm OS/AV cache and power state not isolated; no local build/test during samples",
            "profile instrumentation adds timers/JSON; no cold-cache or speedup claim",
            "native worker_wall includes all child phases; never sum nested times twice",
            "worker residual includes interpreter/bootstrap-before-main and exit/control; not direct spawn-only measurement",
            "interpreter noop is a separate baseline, not a bypassed product launch",
            "memory not measured by this cost-decomposition tool",
        ],
        "median_wall_ms": {
            name: statistics.median(row["wall_ms"] for row in values)
            for name, values in rows.items()
        },
        "raw": rows,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["median_wall_ms"]))


if __name__ == "__main__":
    main()
