"""The vep command-line interface."""

from __future__ import annotations

import json
import os
import re
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
    approve_job,
    authorize_job,
    build_search_index_job,
    default_authoring_skill,
    export_job,
    import_audit_job,
    import_authoring_job,
    import_evidence_job,
    import_revision_job,
    import_transcript_refinement_job,
    initialize_job,
    job_guide,
    job_status,
    normalize_job,
    prepare_audit_job,
    prepare_authoring_job,
    prepare_evidence_job,
    prepare_revision_job,
    prepare_transcript_refinement_job,
    recover_job,
    search_job,
    transcribe_job,
    validate_job,
)
from video_evidence_pipeline.domain.job import (
    PrimaryInputKind,
    SourceKind,
    SupportingInputKind,
    SupportingInputSpec,
    load_job_yaml,
    primary_input_kind,
)
from video_evidence_pipeline.domain.transcription import (
    MAXIMUM_ASR_WORKER_REQUEST_BYTES,
    AsrWorkerRequest,
    AsrWorkerResponse,
    DeviceSelectionReason,
    GapPolicy,
    OverlapPolicy,
    TranscriptionConfig,
)
from video_evidence_pipeline.errors import VepError, configuration_error
from video_evidence_pipeline.schemas.generation import check_schemas, export_schemas

_ASR_CONTAINER_ROOT = Path("/opt/vep")
_ASR_INTERNAL_MARKER = "VEP_ASR_INTERNAL_RUNTIME"
_ASR_CONTRACT_ENV = "VEP_ASR_CONTRACT_SHA256"


def _require_asr_internal_runtime() -> Path:
    contract = os.environ.get(_ASR_CONTRACT_ENV, "")
    profile_registry = _ASR_CONTAINER_ROOT / "config" / "asr-profiles.yaml"
    if (
        os.environ.get(_ASR_INTERNAL_MARKER) != "1"
        or re.fullmatch(r"[0-9a-f]{64}", contract) is None
        or not profile_registry.is_file()
        or profile_registry.is_symlink()
    ):
        raise configuration_error(
            "ASR_INTERNAL_COMMAND_REQUIRED",
            "ASR worker commands are available only inside the repository-owned ASR image",
        )
    return _ASR_CONTAINER_ROOT


def _resolve_baked_worker_profile(container_root: Path, request: AsrWorkerRequest):
    from video_evidence_pipeline.transcription.profiles import resolve_asr_profile

    selected, profile = resolve_asr_profile(container_root, request.profile_id)
    if selected != request.profile_id or (
        request.engine,
        request.engine_version,
        request.runtime,
        request.runtime_version,
        request.model,
        request.model_repository,
        request.model_revision,
        request.gpu_compute_type,
        request.cpu_compute_type,
        request.configured_min_free_vram_mib,
        request.container_image,
    ) != (
        profile.engine,
        profile.engine_version,
        profile.runtime,
        profile.runtime_version,
        profile.model,
        profile.model_repository,
        profile.model_revision,
        profile.gpu_compute_type,
        profile.cpu_compute_type,
        profile.min_free_vram_mib,
        profile.container_image,
    ):
        raise configuration_error(
            "ASR_WORKER_PROFILE_MISMATCH",
            "ASR worker request does not match the baked repository profile",
        )
    return profile


def _verified_worker_provision(request: AsrWorkerRequest, profile):
    from video_evidence_pipeline.transcription.profiles import AsrModelProvisionManifest
    from video_evidence_pipeline.transcription.provisioning import model_payload_identity

    identity, file_count, total_size = model_payload_identity(Path(request.model_path), profile)
    if (
        identity != request.model_storage_identity
        or file_count != request.model_file_count
        or total_size != request.model_total_size_bytes
    ):
        raise configuration_error(
            "ASR_MODEL_INVALID", "mounted ASR model does not match the host provision manifest"
        )
    return AsrModelProvisionManifest(
        profile=request.profile_id,
        model_repository=request.model_repository,
        model_revision=request.model_revision,
        model_storage_identity=request.model_storage_identity,
        file_count=file_count,
        total_size_bytes=total_size,
        provisioned_at=request.model_provisioned_at,
    )


class StructuredTyper(typer.Typer):
    """Normalize Click/Typer parser failures for machine callers."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        exit_code = None
        parser_error = None
        try:
            return super().__call__(*args, standalone_mode=False, **kwargs)
        except typer.Exit as exc:
            exit_code = exc.exit_code
        except (click.ClickException, typer.Abort) as exc:
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
refine_app = typer.Typer(no_args_is_help=True)
index_app = typer.Typer(no_args_is_help=True)
author_app = typer.Typer(no_args_is_help=True)
audit_app = typer.Typer(no_args_is_help=True)
revision_app = typer.Typer(no_args_is_help=True)
asr_app = typer.Typer(no_args_is_help=True)
app.add_typer(job_app, name="job")
app.add_typer(schema_app, name="schema")
app.add_typer(evidence_app, name="evidence")
app.add_typer(refine_app, name="refine")
app.add_typer(index_app, name="index")
app.add_typer(author_app, name="author")
app.add_typer(audit_app, name="audit")
app.add_typer(revision_app, name="revision")
app.add_typer(asr_app, name="asr")


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


def _semantic_exchange_paths(
    workspace_path: Path,
    *,
    request_path: str,
    skill_id: str,
    response_schema: str,
) -> dict[str, str]:
    from video_evidence_pipeline.schemas.structured_output import export_structured_output_schema
    from video_evidence_pipeline.storage.layout import JobWorkspace

    workspace = JobWorkspace.open(workspace_path)
    repository_root = workspace.resources_root
    structured_schema = export_structured_output_schema(
        response_schema,
        workspace.safe_path(f"semantic/structured-output-schemas/{response_schema}"),
    )
    return {
        "request_path": str(workspace.safe_path(request_path).resolve()),
        "skill_path": str(
            (repository_root / ".agents" / "skills" / skill_id / "SKILL.md").resolve()
        ),
        "response_schema_path": str((repository_root / "schemas" / response_schema).resolve()),
        "structured_output_schema_path": str(structured_schema.resolve()),
    }


def _select_authoring_skill(workspace_path: Path, requested: str | None) -> str:
    if requested is not None:
        return requested
    from video_evidence_pipeline.storage.layout import JobWorkspace

    workspace = JobWorkspace.open(workspace_path)
    selected = default_authoring_skill(load_job_yaml(workspace.job_yaml).assignment.task_type)
    if selected is None:
        raise configuration_error(
            "SKILL_SELECTION_REQUIRED",
            "authoring task_type has no exact default Skill; pass --skill explicitly",
        )
    return selected


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


def _supporting_inputs(values: list[str] | None) -> list[SupportingInputSpec]:
    """Parse repeatable ``kind:local-path`` supporting-input options."""

    parsed: list[SupportingInputSpec] = []
    for value in values or []:
        kind_text, delimiter, locator = value.partition(":")
        if not delimiter or not locator.strip():
            raise configuration_error(
                "SUPPORTING_INPUT_INVALID", "supporting input must use kind:local-path"
            )
        try:
            kind = SupportingInputKind(kind_text)
        except ValueError:
            raise configuration_error(
                "SUPPORTING_INPUT_INVALID", "supporting input kind is invalid"
            ) from None
        parsed.append(SupportingInputSpec(kind=kind, locator=locator))
    return parsed


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


@app.command("doctor")
def doctor(
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    media: Annotated[
        bool, typer.Option("--media", help="Also probe local FFmpeg capabilities.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check this source installation without changing jobs or downloading tools."""
    from video_evidence_pipeline.diagnostics import diagnose_installation

    report = diagnose_installation(repository_root, media=media)
    if json_output:
        _emit(report, json_output=True)
    else:
        typer.echo(f"VEP {report['vep_version']} installation checks ({report['mode']})")
        for check in report["checks"]:
            if check["ok"]:
                typer.echo(f"OK {check['name']}")
            else:
                typer.echo(f"FAIL {check['name']}: {check['code']} — {check['remedy']}")
        typer.echo(f"Lock SHA-256: {report['lock_sha256'] or 'unavailable'}")
        typer.echo(f"ASR: {report['asr']}")
    if not report["ok"]:
        raise typer.Exit(code=2)


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
    purpose: Annotated[
        str | None,
        typer.Option("--purpose", help="Intended use that should guide semantic selection."),
    ] = None,
    audience: Annotated[
        str | None,
        typer.Option("--audience", help="Intended reader or viewer of the output."),
    ] = None,
    primary_input: Annotated[
        PrimaryInputKind | None,
        typer.Option("--primary-input", help="Primary input kind; defaults to video."),
    ] = None,
    supporting_input: Annotated[
        list[str] | None,
        typer.Option("--supporting-input", help="Repeat kind:local-path for pdf, image, or notes."),
    ] = None,
    min_chars: Annotated[int | None, typer.Option("--min-chars", min=0)] = None,
    max_chars: Annotated[int | None, typer.Option("--max-chars", min=1)] = None,
    maximum_length_adjustments: Annotated[
        int, typer.Option("--maximum-length-adjustments", min=1, max=10)
    ] = 3,
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
            purpose=purpose,
            audience=audience,
            primary_input=(primary_input if primary_input is not None else PrimaryInputKind.VIDEO),
            supporting_inputs=_supporting_inputs(supporting_input),
            min_chars=min_chars,
            max_chars=max_chars,
            maximum_length_adjustments=maximum_length_adjustments,
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
    deep: Annotated[bool, typer.Option("--deep")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: validate_job(workspace, deep=deep), json_output=json_output)
    _emit({"ok": True, **result}, json_output=json_output)


@job_app.command("recover")
def job_recover(
    parent: Path,
    new_job_id: str,
    basis: Annotated[str, typer.Option("--basis")],
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
    child_spec: Annotated[Path | None, typer.Option("--child-spec")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Fork verified pre-semantic artifacts from a transcript-bearing parent."""

    result = _run(
        lambda: recover_job(
            parent,
            new_job_id,
            confirm=confirm,
            basis=basis,
            child_spec_path=child_spec,
        ),
        json_output=json_output,
    )
    _emit(
        {
            "ok": True,
            "job_id": result.job.job_id,
            "state": "TRANSCRIBED",
            "workspace": str(result.workspace.root),
            "parent_job_id": result.manifest.parent_job_id,
            "parent_finding_ids": result.manifest.parent_finding_ids,
            "recovery_basis": result.manifest.recovery_basis,
        },
        json_output=json_output,
    )


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


@job_app.command("guide")
def guide(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the next safe public operation without changing the workspace."""

    result = _run(lambda: job_guide(workspace), json_output=json_output)
    _emit({"ok": result["status"] != "integrity-error", **result}, json_output=json_output)


@app.command("approve")
def approve(
    workspace: Path,
    approval_target_sha256: Annotated[str, typer.Option("--approval-target-sha256")],
    reviewer: Annotated[str, typer.Option("--reviewer")],
    basis: Annotated[str, typer.Option("--basis")],
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
    unresolved_note: Annotated[
        list[str] | None,
        typer.Option("--unresolved-note", help="Repeat for each unresolved review note."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: approve_job(
            workspace,
            approval_target_sha256=approval_target_sha256,
            reviewer=reviewer,
            basis=basis,
            confirm=confirm,
            unresolved_notes=unresolved_note,
        ),
        json_output=json_output,
    )
    status_payload, _exit_code = _run(
        lambda: job_status(workspace),
        json_output=json_output,
    )
    _emit(
        {
            "ok": True,
            "job_id": result.record.job_id,
            "state": status_payload["stored_state"],
            "approval_id": result.record.approval_id,
            "approval_record_sha256": result.record.record_sha256,
            "approved_draft_version": result.record.approved_draft_version,
            "approval_target_sha256": result.record.approval_target_sha256,
            "reviewer_assertion_method": result.record.reviewer.assertion_method,
            "reviewer_cryptographically_verified": False,
            "reused": result.reused,
        },
        json_output=json_output,
    )


@app.command("export")
def export_delivery(
    workspace: Path,
    export_format: Annotated[str, typer.Option("--format")] = "markdown",
    destination: Annotated[Path | None, typer.Option("--destination")] = None,
    confirm_private_export: Annotated[bool, typer.Option("--confirm-private-export")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if export_format == "study-bundle":
        from video_evidence_pipeline.application import export_study_bundle_job

        def archive():
            if destination is None:
                raise configuration_error(
                    "STUDY_BUNDLE_DESTINATION_REQUIRED", "study-bundle requires --destination"
                )
            return export_study_bundle_job(
                workspace, destination, confirm_private_export=confirm_private_export
            )

        bundle = _run(archive, json_output=json_output)
        _emit(
            {
                "ok": True,
                "job_id": bundle.manifest.job_id,
                "format": "study-bundle",
                "archive_status": "complete",
                "destination": str(bundle.destination),
                "bundle_fingerprint": bundle.manifest.bundle_fingerprint,
                "file_count": len(bundle.manifest.files),
                "reused": bundle.reused,
            },
            json_output=json_output,
        )
        return

    def markdown():
        if export_format != "markdown" or destination is not None or confirm_private_export:
            raise configuration_error(
                "EXPORT_OPTIONS_INVALID", "unsupported format or options for Markdown export"
            )
        return export_job(workspace)

    result = _run(markdown, json_output=json_output)
    _emit(
        {
            "ok": True,
            "job_id": result.manifest.job_id,
            "state": "EXPORTED",
            "format": result.manifest.format,
            "byte_exact": result.manifest.byte_exact,
            "approval_record_sha256": result.manifest.approval_record_sha256,
            "export_fingerprint": result.manifest.export_fingerprint,
            "final_sha256": result.manifest.final_artifact.content_sha256,
            "final_size_bytes": result.manifest.final_artifact.size_bytes,
            "reused": result.reused,
        },
        json_output=json_output,
    )


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
            "frame_count": len(manifest.frame_index.records) if manifest.frame_index else 0,
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
    asr_profile: Annotated[str, typer.Option("--asr-profile")] = "whisper-large-v3",
    asr_device: Annotated[
        str,
        typer.Option("--asr-device"),
    ] = "auto",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = TranscriptionConfig(
        preferred_languages=preferred_language or [],
        minimum_caption_coverage=minimum_caption_coverage,
        gap_policy=gap_policy,
        overlap_policy=overlap_policy,
        asr_language=asr_language,
        asr_profile=asr_profile,
        asr_device=asr_device,
    )
    from video_evidence_pipeline.storage.layout import JobWorkspace
    from video_evidence_pipeline.transcription.container import ContainerAsrBackend

    opened = JobWorkspace.open(workspace)
    repository_root = opened.resources_root
    job = load_job_yaml(opened.job_yaml)
    backend_override = (
        None
        if primary_input_kind(job) is PrimaryInputKind.TRANSCRIPT
        else ContainerAsrBackend(repository_root, config)
    )
    result = _run(
        lambda: transcribe_job(
            workspace,
            config,
            backend_override=backend_override,
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


@asr_app.command("image-build")
def asr_image_build(
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    profile: Annotated[str, typer.Option("--profile")] = "whisper-large-v3",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from video_evidence_pipeline.transcription.container import build_asr_image

    image = _run(
        lambda: build_asr_image(repository_root.resolve(), profile),
        json_output=json_output,
    )
    _emit({"ok": True, "image": image}, json_output=json_output)


@asr_app.command("image-smoke")
def asr_image_smoke(
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    profile: Annotated[str, typer.Option("--profile")] = "whisper-large-v3",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from video_evidence_pipeline.transcription.container import smoke_asr_image

    result = _run(
        lambda: smoke_asr_image(repository_root.resolve(), profile),
        json_output=json_output,
    )
    _emit({"ok": True, **result}, json_output=json_output)


@asr_app.command("gpu-preflight")
def asr_gpu_preflight(
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    profile: Annotated[str, typer.Option("--profile")] = "whisper-large-v3",
    device: Annotated[str, typer.Option("--device")] = "auto",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from video_evidence_pipeline.transcription.container import preflight_asr_gpu

    result = _run(
        lambda: preflight_asr_gpu(repository_root.resolve(), profile, device),
        json_output=json_output,
    )
    _emit({"ok": True, **result}, json_output=json_output)


@asr_app.command("model-provision")
def asr_model_provision(
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    profile: Annotated[str, typer.Option("--profile")] = "whisper-large-v3",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from video_evidence_pipeline.transcription.container import run_provision_container

    def provision_in_container() -> Any:
        raw = run_provision_container(repository_root.resolve(), profile)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise configuration_error(
                "ASR_CONTAINER_RESPONSE_INVALID",
                "ASR provisioning container returned an invalid response",
            ) from None

    payload = _run(provision_in_container, json_output=json_output)
    _emit(payload, json_output=json_output)


@asr_app.command("worker", hidden=True)
def asr_worker(
    request: Annotated[Path, typer.Option("--request")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from video_evidence_pipeline.transcription.device import RuntimeCapabilities
    from video_evidence_pipeline.transcription.faster_whisper import FasterWhisperBackend

    container_root = _run(_require_asr_internal_runtime, json_output=json_output)
    if (
        not request.is_file()
        or request.is_symlink()
        or request.stat().st_size > MAXIMUM_ASR_WORKER_REQUEST_BYTES
    ):
        raise configuration_error("ASR_WORKER_REQUEST_INVALID", "ASR worker request is invalid")
    try:
        worker_request = AsrWorkerRequest.model_validate_json(request.read_bytes())
    except (OSError, ValidationError):
        raise configuration_error(
            "ASR_WORKER_REQUEST_INVALID", "ASR worker request is invalid"
        ) from None
    profile = _run(
        lambda: _resolve_baked_worker_profile(container_root, worker_request),
        json_output=json_output,
    )
    model_path = Path(worker_request.model_path)
    provision = _run(
        lambda: _verified_worker_provision(worker_request, profile),
        json_output=json_output,
    )
    capability_override = None
    if not worker_request.container_gpu_passthrough:
        reason = (
            DeviceSelectionReason(worker_request.gpu_unavailable_reason)
            if worker_request.gpu_unavailable_reason is not None
            else None
        )
        capability_override = RuntimeCapabilities(
            (),
            False,
            False,
            frozenset(),
            unavailable_reason=reason,
        )
    backend = FasterWhisperBackend(
        model_path,
        device=worker_request.requested_device_policy,
        compute_type="auto",
        profile=profile,
        provision_manifest=provision,
        container_image=worker_request.container_image,
        container_image_digest=worker_request.container_image_digest,
        capability_override=capability_override,
    )
    segments = _run(
        lambda: backend.transcribe(
            Path(worker_request.audio_path),
            language=worker_request.language_hint,
            intervals=tuple(worker_request.intervals),
            configuration=worker_request.configuration,
        ),
        json_output=json_output,
    )
    response = AsrWorkerResponse(backend=backend.identity(), segments=segments)
    _emit(response.model_dump(mode="json"), json_output=True)


@asr_app.command("provision-worker", hidden=True)
def asr_provision_worker(
    profile: Annotated[str, typer.Option("--profile")],
    output: Annotated[Path, typer.Option("--output")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from video_evidence_pipeline.transcription.profiles import resolve_asr_profile
    from video_evidence_pipeline.transcription.provisioning import (
        download_model_payload,
        model_payload_identity,
    )

    container_root = _run(_require_asr_internal_runtime, json_output=json_output)
    _selected, baked = _run(
        lambda: resolve_asr_profile(container_root, profile),
        json_output=json_output,
    )
    _run(
        lambda: download_model_payload(output, baked.model_repository, baked.model_revision),
        json_output=json_output,
    )
    _run(lambda: model_payload_identity(output, baked), json_output=json_output)
    _emit({"ok": True, "downloaded": True}, json_output=json_output)


@refine_app.command("prepare")
def refine_prepare(
    workspace: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(lambda: prepare_transcript_refinement_job(workspace), json_output=json_output)
    request = result.request
    paths = _semantic_exchange_paths(
        workspace,
        request_path="refinement/request.json",
        skill_id=(
            request.skill.skill_id if request.skill is not None else "refine-video-transcript"
        ),
        response_schema="refinement-response-manifest.schema.json",
    )
    _emit(
        {
            "ok": True,
            "job_id": request.job_id,
            "request_fingerprint": request.request_fingerprint,
            "transcript_segment_count": len(request.transcript_segments),
            "support_ref_count": len(request.available_source_refs),
            "reused": result.reused,
            **paths,
        },
        json_output=json_output,
    )


@refine_app.command("import")
def refine_import(
    workspace: Path,
    response: Annotated[Path, typer.Option("--response")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: import_transcript_refinement_job(workspace, response),
        json_output=json_output,
    )
    manifest = result.refinement_manifest
    _emit(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": "TRANSCRIPT_REFINED",
            "refined_transcript_fingerprint": manifest.refined_transcript_fingerprint,
            "edit_count": manifest.edit_count,
            "ambiguity_count": manifest.ambiguity_count,
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
    paths = _semantic_exchange_paths(
        workspace,
        request_path="evidence/request.json",
        skill_id=(
            request.skill.skill_id if request.skill is not None else "analyze-video-evidence"
        ),
        response_schema="evidence-response-manifest.schema.json",
    )
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
            **paths,
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


@author_app.command("prepare")
def author_prepare(
    workspace: Path,
    skill: Annotated[str | None, typer.Option("--skill")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    selected_skill = _run(
        lambda: _select_authoring_skill(workspace, skill),
        json_output=json_output,
    )
    result = _run(
        lambda: prepare_authoring_job(workspace, selected_skill),
        json_output=json_output,
    )
    request = result.request
    paths = _semantic_exchange_paths(
        workspace,
        request_path="authoring/requests/author-v001.json",
        skill_id=request.skill.skill_id,
        response_schema="authoring-response-manifest.schema.json",
    )
    _emit(
        {
            "ok": True,
            "job_id": request.job_id,
            "draft_version": request.draft_version,
            "request_fingerprint": request.request_fingerprint,
            "skill_id": request.skill.skill_id,
            "reused": result.reused,
            **paths,
        },
        json_output=json_output,
    )


@author_app.command("import")
def author_import(
    workspace: Path,
    response: Annotated[Path, typer.Option("--response")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: import_authoring_job(workspace, response),
        json_output=json_output,
    )
    manifest = result.manifest
    status, _status_exit_code = job_status(workspace)
    authoring_status = status.get("authoring_integrity", {})
    _emit(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": status["stored_state"],
            "draft_version": authoring_status.get("draft_version", manifest.draft_version),
            "draft_sha256": authoring_status.get(
                "draft_sha256", manifest.draft_artifact.content_sha256
            ),
            "draft_bundle_fingerprint": authoring_status.get(
                "draft_bundle_fingerprint", manifest.bundle_fingerprint
            ),
            "imported_draft_version": manifest.draft_version,
            "imported_draft_sha256": manifest.draft_artifact.content_sha256,
            "reused": result.reused,
        },
        json_output=json_output,
    )


@audit_app.command("prepare")
def audit_prepare(
    workspace: Path,
    skill: Annotated[str, typer.Option("--skill")] = "independent-content-audit",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: prepare_audit_job(workspace, skill),
        json_output=json_output,
    )
    request = result.request
    paths = _semantic_exchange_paths(
        workspace,
        request_path=f"authoring/requests/audit-{request.target_draft_version}.json",
        skill_id=request.skill.skill_id,
        response_schema="audit-response-manifest.schema.json",
    )
    _emit(
        {
            "ok": True,
            "job_id": request.job_id,
            "target_draft_version": request.target_draft_version,
            "request_fingerprint": request.request_fingerprint,
            "skill_id": request.skill.skill_id,
            "reused": result.reused,
            **paths,
        },
        json_output=json_output,
    )


@audit_app.command("import")
def audit_import(
    workspace: Path,
    response: Annotated[Path, typer.Option("--response")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: import_audit_job(workspace, response),
        json_output=json_output,
    )
    manifest = result.manifest
    states = {
        "human_review_ready": "HUMAN_REVIEW_READY",
        "revision_required": "REVISION_REQUIRED",
        "blocked": "BLOCKED",
    }
    _emit(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": states[manifest.disposition.value],
            "target_draft_version": manifest.target_draft_version,
            "audit_bundle_fingerprint": manifest.bundle_fingerprint,
            "disposition": manifest.disposition.value,
            "audit_claim_count": len(result.audit_claims),
            "finding_count": len(result.findings),
            "human_review_bundle": result.human_review is not None,
            "reused": result.reused,
        },
        json_output=json_output,
    )


@revision_app.command("prepare")
def revision_prepare(
    workspace: Path,
    skill: Annotated[str, typer.Option("--skill")] = "revise-from-audit",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: prepare_revision_job(workspace, skill),
        json_output=json_output,
    )
    request = result.request
    paths = _semantic_exchange_paths(
        workspace,
        request_path=f"authoring/requests/revision-{request.next_draft_version}.json",
        skill_id=request.skill.skill_id,
        response_schema="revision-response-manifest.schema.json",
    )
    _emit(
        {
            "ok": True,
            "job_id": request.job_id,
            "target_draft_version": request.target_draft_version,
            "next_draft_version": request.next_draft_version,
            "request_fingerprint": request.request_fingerprint,
            "skill_id": request.skill.skill_id,
            "reused": result.reused,
            **paths,
        },
        json_output=json_output,
    )


@revision_app.command("import")
def revision_import(
    workspace: Path,
    response: Annotated[Path, typer.Option("--response")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = _run(
        lambda: import_revision_job(workspace, response),
        json_output=json_output,
    )
    draft = result.draft.manifest
    manifest = result.manifest
    _emit(
        {
            "ok": True,
            "job_id": manifest.job_id,
            "state": "DRAFTED",
            "draft_version": manifest.revised_draft_version,
            "draft_sha256": draft.draft_artifact.content_sha256,
            "revision_bundle_fingerprint": manifest.bundle_fingerprint,
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


@app.command("bundle-validate")
def bundle_validate(
    bundle: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check a standalone study-bundle without its source workspace."""
    from video_evidence_pipeline.delivery.study_bundle import validate_study_bundle

    manifest = _run(lambda: validate_study_bundle(bundle), json_output=json_output)
    _emit(
        {
            "ok": True,
            "format": manifest.format,
            "job_id": manifest.job_id,
            "bundle_fingerprint": manifest.bundle_fingerprint,
            "file_count": len(manifest.files),
            "input_media_included": False,
            "runtime_replay_supported": False,
        },
        json_output=json_output,
    )
