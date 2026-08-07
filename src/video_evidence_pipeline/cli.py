"""The vep command-line interface."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from typer.core import _click as click

from video_evidence_pipeline import __version__
from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    initialize_job,
    job_status,
    validate_job,
)
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.errors import VepError, configuration_error
from video_evidence_pipeline.schemas.generation import check_schemas, export_schemas


class StructuredTyper(typer.Typer):
    """Normalize Click/Typer parser failures for machine callers."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().__call__(*args, standalone_mode=False, **kwargs)
        except click.exceptions.Exit as exc:
            raise SystemExit(exc.exit_code) from None
        except (click.ClickException, click.exceptions.Abort) as exc:
            if "--json" in sys.argv:
                payload = {
                    "ok": False,
                    "error": {
                        "code": "CONFIG_INVALID",
                        "message": "command arguments are invalid",
                    },
                }
                typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            elif isinstance(exc, click.ClickException):
                exc.show()
            else:
                typer.echo("Aborted!", err=True)
            raise SystemExit(2) from None


app = StructuredTyper(name="vep", no_args_is_help=True, pretty_exceptions_enable=False)
job_app = typer.Typer(no_args_is_help=True)
schema_app = typer.Typer(no_args_is_help=True)
app.add_typer(job_app, name="job")
app.add_typer(schema_app, name="schema")


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, str):
        typer.echo(value)
    else:
        for key, item in value.items():
            typer.echo(f"{key}: {item}")


def _run[T](action: Callable[[], T], *, json_output: bool) -> T:
    try:
        return action()
    except VepError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": exc.safe_message}}
        _emit(payload, json_output=json_output)
        raise typer.Exit(code=int(exc.exit_code)) from None
    except ValidationError as exc:
        error = configuration_error("CONFIG_INVALID", "configuration is invalid")
        payload = {"ok": False, "error": {"code": error.code, "message": error.safe_message}}
        _emit(payload, json_output=json_output)
        raise typer.Exit(code=int(error.exit_code)) from exc


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed version.",
        ),
    ] = False,
) -> None:
    """Operate local video evidence jobs."""


@job_app.command("init")
def job_init(
    job_id: str,
    source_kind: Annotated[SourceKind, typer.Option("--source-kind")],
    source: Annotated[str, typer.Option("--source")],
    task_type: Annotated[str, typer.Option("--task-type")],
    language: Annotated[str, typer.Option("--language")],
    output_format: Annotated[str, typer.Option("--output-format")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: initialize_job(
            Path.cwd(),
            job_id=job_id,
            source_kind=source_kind,
            source=source,
            task_type=task_type,
            language=language,
            output_format=output_format,
        ),
        json_output=json_output,
    )
    workspace, job = result
    _emit(
        {"ok": True, "job_id": job.job_id, "state": "CREATED", "workspace": str(workspace.root)},
        json_output=json_output,
    )


@job_app.command("validate")
def job_validate(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: validate_job(workspace), json_output=json_output)
    _emit({"ok": True, **result}, json_output=json_output)


@job_app.command("authorize")
def job_authorize(
    workspace: Path,
    basis: Annotated[str, typer.Option("--basis")],
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
    allow_local_processing: Annotated[bool, typer.Option("--allow-local-processing")] = False,
    allow_external_api: Annotated[
        bool,
        typer.Option("--allow-external-api/--deny-external-api"),
    ] = False,
    confirmer: Annotated[str | None, typer.Option("--confirmer")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    record = _run(
        lambda: authorize_job(
            workspace,
            confirm=confirm,
            basis=basis,
            allow_local_processing=allow_local_processing,
            allow_external_api=allow_external_api,
            confirmer_identity=confirmer,
        ),
        json_output=json_output,
    )
    _emit(
        {
            "ok": True,
            "job_id": record.job_id,
            "state": "AUTHORIZED",
            "allow_local_processing": record.allow_local_processing,
            "allow_external_api": record.allow_external_api,
        },
        json_output=json_output,
    )


@job_app.command("status")
def status(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result, exit_code = _run(lambda: job_status(workspace), json_output=json_output)
    _emit({"ok": exit_code == 0, **result}, json_output=json_output)
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command("acquire")
def acquire(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: acquire_job(workspace), json_output=json_output)
    artifact = result.source_manifest.artifact_record.artifact
    _emit(
        {
            "ok": True,
            "job_id": result.source_manifest.job_id,
            "state": "ACQUIRED",
            "artifact_id": artifact.artifact_id,
            "content_sha256": artifact.content_sha256,
            "size_bytes": artifact.size_bytes,
            "reused": result.reused,
        },
        json_output=json_output,
    )


@schema_app.command("export")
def schema_export(
    output: Annotated[Path, typer.Option("--output")] = Path("schemas"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    written = _run(lambda: export_schemas(output), json_output=json_output)
    _emit({"ok": True, "written": written}, json_output=json_output)


@schema_app.command("check")
def schema_check(
    root: Annotated[Path, typer.Option("--root")] = Path("schemas"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    checked = _run(lambda: check_schemas(root), json_output=json_output)
    _emit({"ok": True, "checked": checked}, json_output=json_output)


if __name__ == "__main__":
    app()
