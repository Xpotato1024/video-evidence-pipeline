"""Explicit transformations of fresh copied private-runtime inputs only."""

import configparser
import csv
import hashlib
import io
import re
import zipfile
from pathlib import PurePosixPath


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configure_private_module_path(payload):
    path = payload / "runtime/python313._pth"
    original = path.read_bytes()
    active = [
        s.strip()
        for s in original.decode("utf-8-sig").splitlines()
        if s.strip() and not s.strip().startswith("#")
    ]
    if active != ["python313.zip", "."]:
        raise ValueError("unexpected embedded runtime search-path configuration")
    originals = payload / "packaging/runtime-originals"
    originals.mkdir(parents=True, exist_ok=True)
    preserved = originals / path.name
    with preserved.open("xb") as out:
        out.write(original)
    changed = (
        original.decode("utf-8-sig").rstrip()
        + "\n\n# VEP verified private modules; keep site disabled.\n../packages\n../payload/src\n"
    )
    path.write_text(changed, encoding="utf-8", newline="\n")
    return {
        "operation": "configure-private-module-path",
        "path": "runtime/python313._pth",
        "original": preserved.relative_to(payload).as_posix(),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "sha256": digest(path),
        "effective_entries": ["python313.zip", ".", "../packages", "../payload/src"],
        "import_site": False,
    }


def omit_generated_console_launchers(payload):
    """Input RECORD verification must precede copying/this transformation.

    Only a new builder-owned copy may be passed. Preserve original RECORD,
    require declared script names and recognizable uv wrappers, never retarget
    host interpreter paths or silently omit arbitrary upstream executables.
    """
    packages = payload / "packages"
    prepared = []
    removals = set()
    for info in sorted(packages.glob("*.dist-info")):
        ep = info / "entry_points.txt"
        scripts = set()
        if ep.exists():
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(ep.read_text(encoding="utf-8"))
            for section in ("console_scripts", "gui_scripts"):
                if parser.has_section(section):
                    scripts.update(parser[section])
        record = info / "RECORD"
        original = record.read_bytes()
        rows = list(csv.reader(io.StringIO(original.decode("utf-8"))))
        kept, omitted = [], []
        for row in rows:
            if len(row) != 3:
                raise ValueError("invalid copied RECORD")
            relative = PurePosixPath(row[0])
            if relative.is_absolute() or ".." in relative.parts or "\\" in row[0] or ":" in row[0]:
                raise ValueError("unsafe copied RECORD path")
            if relative.parts and relative.parts[0] == "bin":
                if (
                    len(relative.parts) != 2
                    or relative.suffix != ".exe"
                    or relative.stem not in scripts
                ):
                    raise ValueError("unrecognized package bin artifact")
                target = packages / relative
                data = target.read_bytes()
                if not data.startswith(b"MZ") or b"uv_trampoline" not in data:
                    raise ValueError("unrecognized generated script wrapper")
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    if archive.namelist() != ["__main__.py"]:
                        raise ValueError("unrecognized generated wrapper contents")
                canonical = relative.as_posix()
                if canonical in removals:
                    raise ValueError("duplicate generated wrapper owner")
                removals.add(canonical)
                omitted.append(
                    {"path": "packages/" + canonical, "sha256": digest(target), "size": len(data)}
                )
            else:
                kept.append(row)
        if omitted:
            prepared.append((record, original, kept, omitted))
    actual_bin = {
        p.relative_to(packages).as_posix() for p in (packages / "bin").rglob("*") if p.is_file()
    }
    if removals != actual_bin:
        raise ValueError("unlisted generated package bin artifact")
    changes = []
    originals = payload / "packaging/original-records"
    if prepared:
        originals.mkdir(parents=True, exist_ok=True)
    for record, original, kept, omitted in prepared:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", record.parent.name) + ".RECORD"
        preserved = originals / name
        with preserved.open("xb") as out:
            out.write(original)
        for entry in omitted:
            (payload / entry["path"]).unlink()
        with record.open("w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n").writerows(kept)
        changes.append(
            {
                "operation": "omit-generated-console-launcher",
                "record": record.relative_to(payload).as_posix(),
                "original": preserved.relative_to(payload).as_posix(),
                "original_sha256": hashlib.sha256(original).hexdigest(),
                "sha256": digest(record),
                "omitted": omitted,
                "reason": "VEP invokes verified Python modules; installer-generated wrappers are intentionally not distributed.",
            }
        )
    if (packages / "bin").exists():
        (packages / "bin").rmdir()
    if any(packages.rglob("*.exe")):
        raise ValueError("unexpected executable in private modules")
    return changes
