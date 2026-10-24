"""Prepare an exact-input local notice dossier; never grants redistribution approval.

Network is opt-in and limited to public, hash-pinned source archives/notices.
Downloaded code is not executed. Upstream all-target SBOMs are retained as such.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
import tomllib
import urllib.request
from email.parser import Parser
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cached(cache: Path, url: str, expected: str, fetch: bool) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("pinned SHA-256 required")
    target = cache / expected
    if target.exists():
        data = target.read_bytes()
    else:
        if not fetch:
            raise ValueError(f"notice source not cached: {url}")
        if not url.startswith(
            (
                "https://static.crates.io/crates/",
                "https://raw.githubusercontent.com/",
                "https://www.python.org/ftp/",
            )
        ):
            raise ValueError("unapproved notice source")
        request = urllib.request.Request(
            url, headers={"User-Agent": "VEP-local-notice-preparation"}
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("notice source size limit")
        if digest(data) != expected:
            raise ValueError("notice source hash mismatch")
        with target.open("xb") as stream:
            stream.write(data)
    if digest(data) != expected:
        raise ValueError("notice cache hash mismatch")
    return data


class Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def prepare(
    root: Path,
    runtime: Path,
    packages: Path,
    metadata: Path,
    rust_docs: Path,
    cache: Path,
    output: Path,
    fetch: bool = False,
) -> dict:
    if output.exists():
        raise ValueError("fresh dossier output required")
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    records = []
    text = [
        "VEP Windows x64 LOCAL CANDIDATE — third-party notices\n",
        "This dossier is not legal approval or publisher authentication.\n",
        "Preserved upstream SBOMs include other targets; they are not proof of Windows linkage.\n",
    ]

    def notice(
        category,
        component,
        version,
        original,
        data,
        source,
        source_sha=None,
        license_id="NOASSERTION",
        scope="delivered component",
    ):
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("notice file limit")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(original).name)[:100]
        name = f"{category}/{component}-{version}/{len(records):04d}-{safe}"
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record = {
            "component": component,
            "version": version,
            "declared_license": license_id,
            "path": name,
            "sha256": digest(data),
            "source": source,
            "source_sha256": source_sha,
            "original_name": original,
            "scope": scope,
        }
        records.append(record)
        content = data.decode("utf-8", errors="backslashreplace")
        if safe.lower().endswith(".html"):
            visible = Visible()
            visible.feed(content)
            content = "\n".join(visible.parts)
        text.extend(
            [
                f"\n{'=' * 72}\n{component} {version}\n{source}\nOriginal: {name}\nSHA-256: {record['sha256']}\n",
                content,
                "\n",
            ]
        )

    notice(
        "runtime",
        "CPython",
        "3.13.17",
        "LICENSE.txt",
        (runtime / "LICENSE.txt").read_bytes(),
        "https://www.python.org/ftp/python/3.13.17/python-3.13.17-embeddable-amd64.zip",
        "0ba1ee4bfcbdc3a559d27e2e9ce76d04a7839947a23f46375872c755c0d463b8",
        "PSF-2.0",
    )
    catalog = json.loads((root / "native/setup/notice-sources.json").read_text(encoding="utf-8"))
    for item in catalog["sources"]:
        data = cached(cache, item["url"], item["sha256"], fetch)
        notice(
            "runtime",
            "CPython-source-notices",
            "3.13.17",
            item["name"],
            data,
            item["url"],
            item["sha256"],
            scope="upstream notice/SBOM; may include other targets",
        )
    wheel_count = 0
    for info in sorted(packages.glob("*.dist-info")):
        package = Parser().parsestr((info / "METADATA").read_text(encoding="utf-8"))
        name, version = package["Name"], package["Version"]
        license_id = package["License-Expression"] or package["License"] or "NOASSERTION"
        if len(license_id) > 150:
            license_id = "NOASSERTION"
        found = []
        for path in sorted(info.rglob("*")):
            if not path.is_file():
                continue
            lower = path.name.lower()
            if (
                lower.startswith(("license", "copying", "copyright", "notice"))
                or "licenses" in path.parts
                or "sboms" in path.parts
            ):
                found.append(path)
                notice(
                    "wheels",
                    name,
                    version,
                    path.relative_to(info).as_posix(),
                    path.read_bytes(),
                    f"https://pypi.org/project/{name}/{version}/",
                    license_id=license_id,
                )
        if not found:
            raise ValueError(f"wheel has no preserved license: {name}")
        wheel_count += 1
    if wheel_count != 21:
        raise ValueError("expected exactly locked 21 core distributions")
    upstream = json.loads(
        (packages / "pydantic_core-2.46.5.dist-info/sboms/pydantic-core.cyclonedx.json").read_text()
    )
    for component in upstream["components"]:
        name, version = component["name"], component["version"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(
            r"[A-Za-z0-9.+_-]+", version
        ):
            raise ValueError("unsupported source component identifier")
        identity = next(h["content"] for h in component["hashes"] if h["alg"] == "SHA-256")
        source = f"https://static.crates.io/crates/{name}/{name}-{version}.crate"
        data = cached(cache, source, identity, fetch)
        licenses = " OR ".join(
            entry.get("expression", entry.get("license", {}).get("id", "NOASSERTION"))
            for entry in component["licenses"]
        )
        found = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > 20000 or sum(m.size for m in members) > 128 * 1024 * 1024:
                raise ValueError("crate archive inventory limit")
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != f"{name}-{version}"
                ):
                    raise ValueError("unsafe crate archive path")
                if not member.isfile():
                    continue
                relevant = path.name.lower().startswith(
                    ("license", "copying", "copyright", "notice")
                ) or any(p.lower() == "licenses" for p in path.parts)
                if relevant:
                    if member.size > 4 * 1024 * 1024:
                        raise ValueError("crate notice limit")
                    notice(
                        "pydantic-rust",
                        name,
                        version,
                        member.name,
                        archive.extractfile(member).read(),
                        source,
                        identity,
                        licenses,
                        "upstream all-target dependency candidate; Windows linkage not asserted",
                    )
                    found += 1
        if not found:
            raise ValueError(f"source crate has no notice files: {name}@{version}")
    native = json.loads(metadata.read_text(encoding="utf-8-sig"))
    lock = tomllib.loads((root / "native/setup/Cargo.lock").read_text())
    checksums = {(p["name"], p["version"]): p.get("checksum") for p in lock["package"]}
    for package in native["packages"]:
        if package["name"] == "vep-setup":
            continue
        directory = Path(package["manifest_path"]).parent
        found = 0
        for path in sorted(directory.rglob("*")):
            if path.is_file() and (
                path.name.lower().startswith(("license", "copying", "copyright", "notice"))
                or "licenses" in path.relative_to(directory).parts
            ):
                notice(
                    "native",
                    package["name"],
                    package["version"],
                    path.relative_to(directory).as_posix(),
                    path.read_bytes(),
                    f"https://crates.io/crates/{package['name']}/{package['version']}",
                    checksums.get((package["name"], package["version"])),
                    package["license"] or "NOASSERTION",
                )
                found += 1
        if not found:
            raise ValueError(f"native crate has no notice: {package['name']}")
    notice(
        "toolchain",
        "Rust-standard-library",
        "1.94.1",
        "COPYRIGHT-library.html",
        (rust_docs / "COPYRIGHT-library.html").read_bytes(),
        "https://github.com/rust-lang/rust/tree/e408947bfd200af42db322daf0fadfe7e26d3bd1/library",
        scope="toolchain library notices include target-specific material",
    )
    gaps = [
        {
            "id": "MICROSOFT_REDISTRIBUTION_QUALIFICATION",
            "status": "owner-decision-required",
            "detail": "Preserve Microsoft terms in CPython LICENSE; native crt-static also includes MSVC code. Distributor qualification and downstream conditions are not inferred from installed tools.",
        },
        {
            "id": "TARGET_LINKAGE_COVERAGE",
            "status": "not-fully-verified",
            "detail": "Official CPython and pydantic-core SBOMs include other targets; conservative notice collection does not certify exact static linkage. PyYAML/libyaml linkage requires final review.",
        },
    ]
    result = {
        "schema": "vep.notices.v1",
        "distribution_ready": False,
        "wheel_distributions": wheel_count,
        "pydantic_upstream_components": len(upstream["components"]),
        "notices": records,
        "blocking_gaps": gaps,
    }
    (output / "index.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "THIRD_PARTY_NOTICES.txt").write_text("".join(text), encoding="utf-8")
    return {
        "notice_files": len(records),
        "wheel_distributions": wheel_count,
        "pydantic_upstream_components": len(upstream["components"]),
        "blocking_gaps": [g["id"] for g in gaps],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "runtime", "packages", "metadata", "rust-docs", "cache", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--allow-fetch", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.root,
                args.runtime,
                args.packages,
                args.metadata,
                args.rust_docs,
                args.cache,
                args.output,
                args.allow_fetch,
            )
        )
    )


if __name__ == "__main__":
    main()
