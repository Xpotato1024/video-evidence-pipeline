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
from video_evidence_pipeline.acquisition.runtime import (
    AcquisitionRuntimeConfig,
    BrowserCookieSource,
)
from video_evidence_pipeline.application import (
    acquire_job,
    authorize_job,
    build_search_index_job,
    import_evidence_job,
    initialize_job,
    job_status,
    normalize_job,
    prepare_evidence_job,
    search_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.domain.transcription import (
    GapPolicy,
    OverlapPolicy,
    TranscriptionConfig,
)
from video_evidence_pipeline.errors import VepError, configuration_error
from video_evidence_pipeline.schemas.generation import check_schemas, export_schemas


class StructuredTyper(typer.Typer):
    """Normalize Click/Typer parser failures for machine callers."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        exit_code = None
        parser_error = None
        try:
            return super().__call__(*args, standalone_mode=False, **kwargs)
        except click.exceptions.Exit as exc:
            exit_code = exc.exit_code
        except (click.ClickException, click.exceptions.Abort) as exc:
            parser_error = exc
        if exit_code is not None:
            raise SystemExit(exit_code)
        if parser_error is not None:
            if "--json" in sys.argv:
                payload = {
                    "ok": False,
                    "error": {
                        "code": "CONFIG_INVALID",
                        "message": "command arguments are invalid",
                    },
                }
                typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            elif isinstance(parser_error, click.ClickException):
                parser_error.show()
            else:
                typer.echo("Aborted!", err=True)
            raise SystemExit(2)


app = StructuredTyper(name="vep", no_args_is_help=True, pretty_exceptions_enable=False)
job_app = typer.Typer(no_args_is_help=True)
schema_app = typer.Typer(no_args_is_help=True)
evidence_app = typer.Typer(no_args_is_help=True)
index_app = typer.Typer(no_args_is_help=True)
app.add_typer(job_app, name="job")
app.add_typer(schema_app, name="schema")
app.add_typer(evidence_app, name="evidence")
app.add_typer(index_app, name="index")


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, str):
        typer.echo(value)
    else:
        for key, item in value.items():
            typer.echo(f"{key}: {item}")


def _run[T](action: Callable[[], T], *, json_output: bool) -> T:
    error = None
    try:
        result = action()
    except VepError as exc:
        error = exc
    except ValidationError:
        error = configuration_error("CONFIG_INVALID", "configuration is invalid")
    if error is not None:
        payload = {"ok": False, "error": {"code": error.code, "message": error.safe_message}}
        _emit(payload, json_output=json_output)
        raise typer.Exit(code=int(error.exit_code))
    return result


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
    requirement: Annotated[
        list[str] | None,
        typer.Option("--requirement", help="Repeat for each ordered assignment requirement."),
    ] = None,
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
            requirements=requirement,
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
    allow_network_acquisition: Annotated[
        bool,
        typer.Option("--allow-network-acquisition/--deny-network-acquisition"),
    ] = False,
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
            allow_network_acquisition=allow_network_acquisition,
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
            "allow_network_acquisition": record.allow_network_acquisition,
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
    cookies_from_browser: Annotated[
        BrowserCookieSource | None,
        typer.Option("--cookies-from-browser"),
    ] = None,
    box_token_env: Annotated[str | None, typer.Option("--box-token-env")] = None,
    panopto_token_env: Annotated[str | None, typer.Option("--panopto-token-env")] = None,
    panopto_tenant_host: Annotated[str | None, typer.Option("--panopto-tenant-host")] = None,
    panopto_ytdlp_fallback: Annotated[
        bool,
        typer.Option("--panopto-ytdlp-fallback"),
    ] = False,
    network_timeout: Annotated[float, typer.Option("--network-timeout")] = 30.0,
    maximum_download_seconds: Annotated[float, typer.Option("--maximum-download-seconds")] = 3600.0,
    maximum_download_bytes: Annotated[
        int,
        typer.Option("--maximum-download-bytes"),
    ] = 2 * 1024 * 1024 * 1024,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    def acquire_with_runtime_config():
        runtime = AcquisitionRuntimeConfig(
            cookies_from_browser=cookies_from_browser,
            box_token_env=box_token_env,
            panopto_token_env=panopto_token_env,
            panopto_tenant_host=panopto_tenant_host,
            panopto_ytdlp_fallback=panopto_ytdlp_fallback,
            network_timeout_seconds=network_timeout,
            maximum_download_seconds=maximum_download_seconds,
            maximum_download_bytes=maximum_download_bytes,
        )
        return acquire_job(workspace, runtime)

    result = _run(acquire_with_runtime_config, json_output=json_output)
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


@app.command("normalize")
def normalize(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: normalize_job(workspace), json_output=json_output)
    manifest = result.media_manifest
    _emit(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": "NORMALIZED",
            "normalization_fingerprint": manifest.normalization_fingerprint,
            "frame_count": len(manifest.frame_index.records),
            "subtitle_count": len(manifest.subtitles),
            "speech_audio": manifest.speech_audio is not None,
            "warnings": manifest.warnings,
            "reused": result.reused,
        },
        json_output=json_output,
    )


@app.command("transcribe")
def transcribe(
    workspace: Path,
    preferred_language: Annotated[
        list[str] | None,
        typer.Option("--preferred-language", help="Repeat in deterministic preference order."),
    ] = None,
    minimum_caption_coverage: Annotated[
        float,
        typer.Option("--minimum-caption-coverage", min=0.0, max=1.0),
    ] = 0.8,
    gap_policy: Annotated[GapPolicy, typer.Option("--gap-policy")] = GapPolicy.RECORD,
    overlap_policy: Annotated[OverlapPolicy, typer.Option("--overlap-policy")] = (
        OverlapPolicy.PRESERVE
    ),
    asr_language: Annotated[str | None, typer.Option("--asr-language")] = None,
    asr_model_directory: Annotated[
        Path | None,
        typer.Option(
            "--asr-model-directory",
            help="Existing local faster-whisper model directory; never downloaded.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = TranscriptionConfig(
        preferred_languages=preferred_language or [],
        minimum_caption_coverage=minimum_caption_coverage,
        gap_policy=gap_policy,
        overlap_policy=overlap_policy,
        asr_language=asr_language,
    )
    result = _run(
        lambda: transcribe_job(
            workspace,
            config,
            asr_model_directory=asr_model_directory,
        ),
        json_output=json_output,
    )
    manifest = result.transcript_manifest
    _emit(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": "TRANSCRIBED",
            "transcription_fingerprint": manifest.transcription_fingerprint,
            "segment_count": manifest.segment_count,
            "gap_count": len(manifest.gaps),
            "overlap_count": len(manifest.overlaps),
            "sources": [item.source.value for item in manifest.backends],
            "reused": result.reused,
        },
        json_output=json_output,
    )


@evidence_app.command("prepare")
def evidence_prepare(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: prepare_evidence_job(workspace), json_output=json_output)
    request = result.request
    _emit(
        {
            "ok": True,
            "job_id": request.job_id,
            "request_fingerprint": request.request_fingerprint,
            "transcript_segment_count": len(request.transcript_segments),
            "transcript_ref_count": len(request.transcript_refs),
            "frame_ref_count": len(request.frame_refs),
            "unresolved_ambiguity_count": len(request.unresolved_ambiguities),
            "reused": result.reused,
        },
        json_output=json_output,
    )


@evidence_app.command("import")
def evidence_import(
    workspace: Path,
    response: Annotated[Path, typer.Option("--response")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: import_evidence_job(workspace, response),
        json_output=json_output,
    )
    manifest = result.evidence_manifest
    _emit(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": "EVIDENCE_READY",
            "evidence_bundle_fingerprint": manifest.evidence_bundle_fingerprint,
            "evidence_count": manifest.evidence_count,
            "ambiguity_count": manifest.ambiguity_count,
            "reused": result.reused,
        },
        json_output=json_output,
    )


@index_app.command("build")
def index_build(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: build_search_index_job(workspace), json_output=json_output)
    metadata = result.metadata
    _emit(
        {
            "ok": True,
            "job_id": metadata.job_id,
            "semantic_fingerprint": metadata.semantic_fingerprint,
            "transcript_count": metadata.transcript_count,
            "evidence_count": metadata.evidence_count,
            "tokenizer": metadata.tokenizer,
            "reused": result.reused,
        },
        json_output=json_output,
    )


@app.command("search")
def search(
    workspace: Path,
    query: str,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: search_job(workspace, query, limit), json_output=json_output)
    payload = result.model_dump(mode="json")
    _emit({"ok": True, **payload}, json_output=json_output)


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
