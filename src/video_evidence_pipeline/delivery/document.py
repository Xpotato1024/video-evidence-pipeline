"""Render an approved Markdown snapshot to one local delivery format."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from html import escape
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from markdown_it import MarkdownIt

from video_evidence_pipeline.acquisition.process_tree import ManagedProcessTree
from video_evidence_pipeline.application import read_approved_markdown_job
from video_evidence_pipeline.domain.hashing import sha256_bytes, sha256_file
from video_evidence_pipeline.domain.serialization import pretty_json_bytes
from video_evidence_pipeline.errors import VepError, configuration_error, integrity_error, io_error
from video_evidence_pipeline.runtime_context import resource_root
from video_evidence_pipeline.storage.atomic import atomic_write_new_bytes
from video_evidence_pipeline.storage.layout import JobWorkspace, is_link_or_junction
from video_evidence_pipeline.storage.locking import exclusive_file_lock

_RECEIPT = "render-receipt.json"
_STYLES = """
@page { size: A4; margin: 16mm 18mm; }
body { margin: 2rem auto; padding: 0 1.25rem; max-width: 46rem; color: #182026;
  font: 16px/1.55 system-ui, "Noto Sans CJK JP", sans-serif; overflow-wrap: anywhere; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; break-after: avoid; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th, td { border: 1px solid #a9b3ba; padding: .45rem .65rem; vertical-align: top; }
th { background: #edf1f4; }
blockquote { margin-left: 0; padding-left: 1rem; border-left: 2px solid #a9b3ba; }
pre { white-space: pre-wrap; }
@media print { body { font-size: 10.5pt; margin: 0; padding: 0; } tr { break-inside: avoid; } }
"""


def _image_as_text(_renderer, tokens, index, _options, _environment) -> str:
    return escape(tokens[index].content)


def render_markdown_html(payload: bytes, language: str) -> bytes:
    """Parse Markdown without executing raw HTML or loading embedded images."""
    try:
        source = payload.decode("utf-8")
    except UnicodeError as exc:
        raise integrity_error("APPROVAL_STALE", "approved Markdown is not valid UTF-8") from exc
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    parser.add_render_rule("image", _image_as_text)
    rendered = parser.render(source)
    return f"""<!doctype html>
<html lang="{escape(language, quote=True)}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<title>VEP document</title>
<style>{_STYLES}</style>
<main>
{rendered}</main>
</html>
""".encode()


def _publish_pdf(html: bytes, destination: Path, scratch: Path, script: Path) -> None:
    node = shutil.which("node")
    if node is None or not script.is_file():
        raise configuration_error(
            "DOCUMENT_PDF_RENDERER_MISSING", "install Node.js and the VEP browser renderer for PDF"
        )
    input_path = scratch / "print.html"
    atomic_write_new_bytes(input_path, html, private=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "PLAYWRIGHT_BROWSERS_PATH",
            "VEP_CHROMIUM_PATH",
            "VEP_CHROMIUM_SANDBOX",
            "LD_LIBRARY_PATH",
            "XDG_CACHE_HOME",
            "FONTCONFIG_PATH",
        }
    }
    env["TZ"] = "UTC"
    try:
        process = ManagedProcessTree.start(
            [node, str(script), str(input_path), str(destination)],
            process_factory=subprocess.Popen,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=scratch,
            env=env,
            shell=False,
        )
    except OSError as exc:
        raise io_error("DOCUMENT_PDF_RENDER_FAILED", "PDF renderer could not start") from exc
    try:
        if process.wait(timeout=180):
            raise configuration_error(
                "DOCUMENT_PDF_RENDER_FAILED",
                "PDF rendering failed; check the Chromium installation",
            )
    except subprocess.TimeoutExpired as exc:
        process.stop()
        raise io_error(
            "DOCUMENT_PDF_RENDER_TIMEOUT", "PDF rendering exceeded its deadline"
        ) from exc
    finally:
        process.close()


def _artifact_metadata(path: Path, output_format: str) -> dict[str, str | int]:
    metadata = path.stat(follow_symlinks=False)
    if (
        is_link_or_junction(path)
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 < metadata.st_size <= 64 * 1024 * 1024
    ):
        raise integrity_error("DOCUMENT_OUTPUT_INVALID", "rendered document file is invalid")
    with path.open("rb") as stream:
        prefix = b"%PDF-" if output_format == "pdf" else b"<!doctype html>"
        if stream.read(len(prefix)) != prefix:
            raise integrity_error("DOCUMENT_OUTPUT_INVALID", "rendered document format is invalid")
    return {"path": path.name, "sha256": sha256_file(path), "size_bytes": metadata.st_size}


def _read_receipt(path: Path) -> bytes:
    if is_link_or_junction(path) or not path.is_file():
        raise ValueError("unavailable render receipt")
    with path.open("rb") as stream:
        return stream.read(8_193)


def _pdf_binding(job: JobWorkspace, receipt: bytes) -> Path:
    return job.safe_path(f"exports/derived-pdf/{sha256_bytes(receipt)}.json")


def _record_pdf_binding(job: JobWorkspace, receipt: bytes) -> None:
    path = _pdf_binding(job, receipt)
    try:
        atomic_write_new_bytes(path, receipt, private=True)
    except VepError as exc:
        if exc.code != "FILE_EXISTS":
            raise
        try:
            if _read_receipt(path) == receipt:
                return
        except (OSError, ValueError):
            pass
        raise integrity_error(
            "DOCUMENT_PDF_BINDING_CONFLICT", "stored PDF binding differs"
        ) from exc


def render_document_job(workspace: Path, destination: Path, output_format: str) -> dict:
    """Derive a selected format and record the approved source and output hashes."""
    if output_format not in {"html", "pdf"}:
        raise configuration_error("DOCUMENT_FORMAT_UNSUPPORTED", "choose html or pdf")
    job = JobWorkspace.open(workspace)
    destination = destination.absolute()
    if any(
        is_link_or_junction(path) for path in (destination, *destination.parents)
    ) or destination.resolve().is_relative_to(job.root.resolve()):
        raise configuration_error(
            "DOCUMENT_DESTINATION_UNSAFE",
            "use an export directory outside the job workspace and without links",
        )
    destination = destination.resolve()
    if not destination.parent.is_dir():
        raise configuration_error("DOCUMENT_PARENT_MISSING", "create the destination parent first")

    record, language, payload = read_approved_markdown_job(workspace)
    html = render_markdown_html(payload, language)
    script = resource_root(Path(__file__).resolve().parents[3]) / "renderer" / "print-document.mjs"
    renderer = {"markdown_it_py": version("markdown-it-py")}
    if output_format == "pdf":
        if not script.is_file():
            raise configuration_error(
                "DOCUMENT_PDF_RENDERER_MISSING", "install the VEP browser renderer for PDF"
            )
        try:
            tools = {
                entry: sha256_file(script.with_name(entry))
                for entry in (script.name, "browser.mjs", "common.mjs", "package-lock.json")
            }
        except OSError as exc:
            raise configuration_error(
                "DOCUMENT_PDF_RENDERER_MISSING", "install the complete VEP browser renderer"
            ) from exc
        renderer["print_source_sha256"] = sha256_bytes(pretty_json_bytes(tools))
    name = f"document.{output_format}"
    receipt = {
        "schema_version": "1.0",
        "job_id": record.job_id,
        "approval_record_sha256": record.record_sha256,
        "approved_markdown_sha256": record.approved_draft_artifact.content_sha256,
        "rendered_html_sha256": sha256_bytes(html),
        "format": output_format,
        "renderer": renderer,
    }
    with exclusive_file_lock(
        destination.with_name(f".{destination.name}.vep-render.lock"),
        busy_code="DOCUMENT_DESTINATION_BUSY",
        busy_message="document destination is in use",
        failure_code="DOCUMENT_LOCK_FAILED",
        failure_message="document destination lock failed",
    ):
        reused = destination.exists()
        if reused:
            try:
                if (
                    is_link_or_junction(destination)
                    or not destination.is_dir()
                    or {item.name for item in destination.iterdir()} != {name, _RECEIPT}
                ):
                    raise ValueError
                artifact = _artifact_metadata(destination / name, output_format)
                bound = pretty_json_bytes({**receipt, "artifact": artifact})
                if _read_receipt(destination / _RECEIPT) != bound or (
                    output_format == "html"
                    and receipt["rendered_html_sha256"] != artifact["sha256"]
                ):
                    raise ValueError
                if output_format == "pdf" and _read_receipt(_pdf_binding(job, bound)) != bound:
                    raise ValueError
            except (OSError, ValueError) as exc:
                raise integrity_error(
                    "DOCUMENT_DESTINATION_CONFLICT",
                    "existing destination does not match this render; choose a new directory",
                ) from exc
        else:
            try:
                with TemporaryDirectory(prefix=".vep-render-", dir=destination.parent) as temp:
                    scratch = Path(temp)
                    stage = scratch / "payload"
                    stage.mkdir(mode=0o700)
                    output = stage / name
                    if output_format == "html":
                        atomic_write_new_bytes(output, html, private=True)
                    else:
                        _publish_pdf(html, output, scratch, script)
                    artifact = _artifact_metadata(output, output_format)
                    bound = pretty_json_bytes({**receipt, "artifact": artifact})
                    if output_format == "pdf":
                        _record_pdf_binding(job, bound)
                    atomic_write_new_bytes(stage / _RECEIPT, bound, private=True)
                    if destination.exists() or is_link_or_junction(destination):
                        raise integrity_error(
                            "DOCUMENT_DESTINATION_CONFLICT", "document destination already exists"
                        )
                    stage.rename(destination)
            except OSError as exc:
                raise io_error("DOCUMENT_WRITE_FAILED", "could not publish the render") from exc
    return {
        "ok": True,
        "job_id": record.job_id,
        "format": output_format,
        "destination": str(destination),
        "artifact": str(destination / name),
        "sha256": artifact["sha256"],
        "approval_record_sha256": record.record_sha256,
        "reused": reused,
    }
