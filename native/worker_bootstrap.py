"""Isolated entrypoint, included in the local development payload inventory."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    started = time.perf_counter()
    release = Path(__file__).resolve().parent.parent
    # -I -S and the embedded ._pth exclude cwd, user site and PYTHONPATH.
    sys.path[:0] = [str(release / "payload" / "src"), str(release / "packages")]
    from video_evidence_pipeline.cli import app
    from video_evidence_pipeline.runtime_context import (
        DataContext,
        ResourceContext,
        RuntimeContext,
        use_runtime,
    )

    imports_finished = time.perf_counter()

    path = Path(sys.argv[1])
    if path.stat().st_size > 65536:
        raise SystemExit("RUNTIME_CONTEXT_INVALID")
    context = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema",
        "release_id",
        "resource_root",
        "data_root",
        "bindings_root",
        "app_root",
        "state_root",
    }
    if (
        set(context) != expected
        or context["schema"] != "vep.runtime-context.v1"
        or Path(context["resource_root"]).resolve() != release / "payload"
    ):
        raise SystemExit("RUNTIME_CONTEXT_INVALID")
    runtime = RuntimeContext(
        context["release_id"],
        ResourceContext(Path(context["resource_root"])),
        DataContext(Path(context["data_root"]), Path(context["bindings_root"])),
        (Path(context["app_root"]), Path(context["state_root"])),
    )
    with use_runtime(runtime):
        context_finished = time.perf_counter()
        # StructuredTyper may return a nonzero Click exit code in embedded mode.
        # Console entrypoints call sys.exit(app()); preserve that exact contract.
        try:
            raise SystemExit(app(args=sys.argv[2:], prog_name="vep"))
        finally:
            if os.environ.get("VEP_PROFILE_LAUNCH") == "1":
                print(
                    json.dumps(
                        {
                            "schema": "vep.launch-profile.v1",
                            "scope": "worker",
                            "timings": {
                                "bootstrap_imports_ms": (imports_finished - started) * 1000,
                                "context_ms": (context_finished - imports_finished) * 1000,
                                "cli_dispatch_and_doctor_ms": (
                                    time.perf_counter() - context_finished
                                )
                                * 1000,
                            },
                        }
                    ),
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
