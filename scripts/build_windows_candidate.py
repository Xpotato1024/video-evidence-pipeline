"""Assemble a LOCAL unsigned offline acceptance fixture; never publish/install.

Uses only explicit runtime/packages/native/notices inputs. Distribution remains
blocked by unresolved qualification, signature and clean-user acceptance gates.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tomllib
import zipfile
from datetime import UTC, datetime
from email.parser import Parser
from pathlib import Path, PurePosixPath

from build_private_runtime import assert_plain, assert_plain_tree, build, canonical_destination
from windows_candidate_transforms import (
    configure_private_module_path,
    omit_generated_console_launchers,
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def verify_pe_x64(path):
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("Windows x64 PE input required")
    offset = struct.unpack_from("<I", data, 60)[0]
    if (
        offset > len(data) - 26
        or data[offset : offset + 4] != b"PE\0\0"
        or (
            struct.unpack_from("<H", data, offset + 4)[0] != 0x8664
            or struct.unpack_from("<H", data, offset + 24)[0] != 0x20B
        )
    ):
        raise ValueError("Windows x64 PE input required")


def verify_source_allowlist(root, paths):
    allowlist = set(paths)
    for directory in ("src", ".agents", "config", "schemas", "containers/asr"):
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if (
                path.is_file()
                and "__pycache__" not in relative.parts
                and path.suffix != ".pyc"
                and relative.as_posix() not in allowlist
            ):
                raise ValueError(
                    "untracked runtime source input would be omitted from source snapshot"
                )


def verify_notice_inventory(notices, dossier):
    expected = {n["path"] for n in dossier["notices"]} | {"index.json", "THIRD_PARTY_NOTICES.txt"}
    actual = {p.relative_to(notices).as_posix() for p in notices.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("unlisted notice input")


def verify_packages(root: Path, packages: Path) -> list:
    """Check installed RECORD bytes against locked distribution identities.

    RECORD is not publisher authentication. The preparation command must still
    use --require-hashes; preserve the lock's artifact URLs/hashes for review.
    """
    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))["package"]
    components, recorded = [], set()
    for info in sorted(packages.glob("*.dist-info")):
        metadata = Parser().parsestr((info / "METADATA").read_text(encoding="utf-8"))
        name, version = metadata["Name"], metadata["Version"]

        def normalize(s):
            return re.sub(r"[-_.]+", "-", s).lower()

        entry = next(
            (
                p
                for p in locked
                if normalize(p["name"]) == normalize(name) and p["version"] == version
            ),
            None,
        )
        if entry is None:
            raise ValueError("distribution is not locked")
        for relative, checksum, size in csv.reader(
            (info / "RECORD").open(encoding="utf-8", newline="")
        ):
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
                raise ValueError("unsafe wheel RECORD path")
            target = packages.joinpath(*path.parts)
            assert_plain(target)
            if checksum:
                if not checksum.startswith("sha256="):
                    raise ValueError("unsupported RECORD hash")
                actual = base64.urlsafe_b64encode(bytes.fromhex(sha(target))).decode().rstrip("=")
                if checksum[7:] != actual or target.stat().st_size != int(size):
                    raise ValueError("wheel RECORD mismatch")
            elif target != info / "RECORD":
                raise ValueError("unhashed wheel file")
            recorded.add(target.relative_to(packages).as_posix())
        components.append(
            {
                "name": name,
                "version": version,
                "license": metadata["License-Expression"] or "NOASSERTION",
                "locked_artifacts": [entry.get("sdist"), *entry.get("wheels", [])],
            }
        )
    actual = {p.relative_to(packages).as_posix() for p in packages.rglob("*") if p.is_file()}
    # uv target preparation retains one empty coordination file, not a module.
    if ".lock" in actual and (packages / ".lock").stat().st_size == 0:
        actual.remove(".lock")
    if recorded != actual or len(components) != 21:
        raise ValueError("unexpected package inventory")
    return components


def assemble(root, archive, expected, packages, native, notices, output, release):
    root, archive, packages, native, notices, output = (
        Path(os.path.abspath(p)) for p in (root, archive, packages, native, notices, output)
    )
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", release):
        raise ValueError("simple candidate release identifier required")
    if output.exists():
        raise ValueError("fresh output required")
    for parent in output.parents:
        if parent.exists():
            assert_plain(parent)
    destination = canonical_destination(output)
    for item in (root, packages, native, notices):
        source = item.resolve(strict=True)
        if destination.is_relative_to(source) or source.is_relative_to(destination):
            raise ValueError("candidate output must not overlap any input tree")
    for item in (native, notices, packages):
        assert_plain_tree(item)
    for name in ("vep-setup.exe", "vep-setup-gui.exe"):
        assert_plain(native / name)
        verify_pe_x64(native / name)
    dossier = json.loads((notices / "index.json").read_text(encoding="utf-8"))
    if dossier["schema"] != "vep.notices.v1" or dossier["distribution_ready"] is not False:
        raise ValueError("local notice dossier required")
    for entry in dossier["notices"]:
        relative = PurePosixPath(entry["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or sha(notices / relative) != entry["sha256"]
        ):
            raise ValueError("notice inventory mismatch")
    verify_notice_inventory(notices, dossier)
    tracked = (
        subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"]).decode().split("\0")
    )
    # Explicit additions only: never sweep arbitrary untracked files into source.
    additions = [
        "scripts/build_windows_candidate.py",
        "tests/test_windows_candidate.py",
        "tests/test_user_install_gui.py",
        "scripts/windows_candidate_transforms.py",
        "tests/test_windows_candidate_transforms.py",
    ]
    paths = sorted(set(p for p in [*tracked, *additions] if p and (root / p).is_file()))
    verify_source_allowlist(root, paths)
    components = verify_packages(root, packages)
    output.mkdir(parents=True)
    payload = output / "payload"
    build(root, archive, expected, packages, payload, release)
    preparation_lock = payload / "packages/.lock"
    if preparation_lock.exists() and preparation_lock.stat().st_size == 0:
        preparation_lock.unlink()
    transformations = omit_generated_console_launchers(payload)
    transformations.append(configure_private_module_path(payload))
    verify_packages(root, payload / "packages")
    write_json(
        payload / "packaging/transforms.json",
        {
            "schema": "vep.packaging-transforms.v1",
            "changes": transformations,
            "input_records_verified_before_copy": True,
            "output_records_verified_after_transform": True,
        },
    )
    (payload / "tools").mkdir()
    for name in ("vep-setup.exe", "vep-setup-gui.exe"):
        shutil.copy2(native / name, payload / "tools" / name)
        shutil.copy2(native / name, output / name)
    shutil.copytree(notices, payload / "licenses")
    source = payload / "source"
    source.mkdir()
    with zipfile.ZipFile(source / "vep-source.zip", "x", compression=zipfile.ZIP_DEFLATED) as z:
        for relative in paths:
            assert_plain(root / relative)
            z.write(root / relative, "video-evidence-pipeline/" + relative)
    base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    write_json(
        payload / "provenance.json",
        {
            "schema": "vep.local-candidate.v1",
            "source_base_commit": base,
            "source_kind": "working-tree snapshot; exact bytes identified by source_sha256",
            "source_sha256": sha(source / "vep-source.zip"),
            "runtime_sha256": expected,
            "package_integrity": "Verified: RECORD self consistency and Name/Version in uv.lock. Exact wheel archive identity requires separate preparation evidence.",
            "python_packages": components,
            "native_sha256": {n: sha(native / n) for n in ("vep-setup.exe", "vep-setup-gui.exe")},
            "distribution_ready": False,
            "publisher_authenticated": False,
            "native_build_kind": "Not inferred by builder; supplied inputs require separate compiler/build evidence. Never distribute this local fixture.",
            "blocking_gaps": [
                *dossier["blocking_gaps"],
                {"id": "SIGNATURE", "status": "unsigned"},
                {"id": "CLEAN_STANDARD_USER_ACCEPTANCE", "status": "not-run"},
            ],
            "optional_components_included": [],
        },
    )
    entries = [
        {"path": p.relative_to(payload).as_posix(), "size": p.stat().st_size, "sha256": sha(p)}
        for p in sorted(payload.rglob("*"))
        if p.is_file()
    ]
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "VEP local Windows candidate " + release,
        "documentNamespace": "https://spdx.org/spdxdocs/vep-local-"
        + release
        + "-"
        + sha(source / "vep-source.zip"),
        "creationInfo": {
            "creators": ["Tool: VEP-local-candidate-builder"],
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "documentDescribes": ["SPDXRef-LocalPayload"],
        "packages": [
            {
                "name": "VEP-local-payload",
                "SPDXID": "SPDXRef-LocalPayload",
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        ],
        "files": [
            {
                "SPDXID": f"SPDXRef-File-{i}",
                "fileName": "./" + e["path"],
                "checksums": [{"algorithm": "SHA256", "checksumValue": e["sha256"]}],
                "licenseConcluded": "NOASSERTION",
                "licenseInfoInFiles": ["NOASSERTION"],
                "copyrightText": "NOASSERTION",
            }
            for i, e in enumerate(entries)
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-LocalPayload",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": f"SPDXRef-File-{i}",
            }
            for i in range(len(entries))
        ],
        "comment": "Exact file inventory, excluding this self-referential document. Upstream SBOMs in licenses include other targets; NOASSERTION is not licensing clearance.",
    }
    write_json(payload / "SBOM.spdx.json", sbom)
    entries.append(
        {
            "path": "SBOM.spdx.json",
            "size": (payload / "SBOM.spdx.json").stat().st_size,
            "sha256": sha(payload / "SBOM.spdx.json"),
        }
    )
    manifest = {
        "schema": "vep.sandbox-release.v1",
        "release_id": release,
        "target": "windows-x86_64",
        "python": "runtime/python.exe",
        "files": sorted(entries, key=lambda e: e["path"]),
    }
    write_json(output / "payload.manifest.json", manifest)
    write_json(
        output / "bundle.json",
        {
            "schema": "vep.offline-bundle.v1",
            "manifest": "payload.manifest.json",
            "payload": "payload",
            "manifest_sha256": sha(output / "payload.manifest.json"),
            "distribution_ready": False,
            "publisher_authenticated": False,
        },
    )
    (output / "LOCAL-CANDIDATE.txt").write_text(
        "Unsigned LOCAL test fixture. Do not distribute. No default-location installation is authorized by preparation.\n"
        "Use an explicit fresh temporary --user-root. Inspect plan and licenses before apply.\n"
        "No FFmpeg, ASR/model, online updater, PATH/registry change or user-data deletion.\n"
        "Microsoft redistribution qualification, static linkage coverage, signing and clean-user acceptance remain gates.\n",
        encoding="utf-8",
    )
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{sha(output / n)}  {n}\n"
            for n in ("vep-setup.exe", "vep-setup-gui.exe", "payload.manifest.json", "bundle.json")
        ),
        encoding="ascii",
    )
    return {
        "output": str(output),
        "manifest_sha256": sha(output / "payload.manifest.json"),
        "files": len(entries),
        "expanded_bytes": sum(e["size"] for e in entries),
        "distribution_ready": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "runtime-archive", "packages", "native", "notices", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--runtime-sha256", required=True)
    p.add_argument("--release-id", required=True)
    a = p.parse_args()
    print(
        json.dumps(
            assemble(
                a.root,
                a.runtime_archive,
                a.runtime_sha256,
                a.packages,
                a.native,
                a.notices,
                a.output,
                a.release_id,
            )
        )
    )


if __name__ == "__main__":
    main()
