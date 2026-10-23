# Installation, distribution and updates

The experimental native installer/launcher and private runtime are documented in
[the local development record](native-install-development.md). They are not yet a supported installer or distribution.

## Install the complete checkout

VEP currently needs `src`, `.agents` (including promotion evidence), `config`, `schemas`,
`containers`, and `uv.lock` from the same revision. Run commands from that checkout.
The CLI version alone (`0.1.0` on the baseline) does not identify these resources;
record the full commit ID and lock hash as well. A wheel or `uv tool install` alone
does not provide the complete operational installation. Freezing only the CLI into
an executable would retain this problem and would not remove FFmpeg, Docker or model costs.

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) using
your approved OS package source. CI pins uv 0.10.0 and Python 3.12; Python 3.13 is
allowed by the manifest but is not covered by the current CI. Do not change system
Python, security settings or execution policy to make installation pass.

These commands work as separate one-line commands in PowerShell and POSIX shells:

```text
git clone https://github.com/Xpotato1024/video-evidence-pipeline.git vep
cd vep
git switch --detach FULL_REVIEWED_COMMIT_ID
uv sync --locked --no-dev --python 3.12
uv run --no-sync vep doctor --json
uv run --no-sync vep --version
git rev-parse HEAD
```

Replace `FULL_REVIEWED_COMMIT_ID` with an owner-approved 40-character commit ID.
Public source can be cloned without credentials. Do not paste tokens into clone URLs. `uv sync` may
download Python and packages; it does not download FFmpeg or an ASR model.
`--no-dev` avoids installing pytest/ruff on an operator machine. Use
`uv sync --locked` for development. Keep `--no-sync` on operational `uv run`
commands after the explicit sync; otherwise uv may install the default dev group.
The [uv sync documentation](https://docs.astral.sh/uv/concepts/projects/sync/)
defines these options. Existing short examples assume an already prepared environment.

`doctor` is read-only: it checks Python, the matching editable checkout, required
resources, active semantic Skill contracts and schema drift. Its JSON contains
stable check codes, remedies, CLI version and lock SHA-256. Exit 0 means these
installation checks passed; exit 2 means a prerequisite failed. It reads no job,
source, credentials or media. `--media` additionally executes bounded local
FFmpeg capability probes. Neither form provisions tools/models or proves ASR readiness.

## Choose dependencies by input and OS

| Path / OS | Required | Validation and limit |
| --- | --- | --- |
| Supplied transcript, all OS | Python + uv + complete checkout | `vep doctor`; FFmpeg/Docker/GPU/model unnecessary |
| Windows x64 media | pinned ffmpeg/ffprobe pair | `powershell -NoProfile -File scripts/install_ffmpeg.ps1`, then `vep doctor --media`; installer downloads the manifest-pinned archive and does not change PATH unless explicitly requested |
| Linux media | distribution FFmpeg with VEP capabilities | Ubuntu example: `sudo apt-get install ffmpeg`; then `vep doctor --media`; package version alone is insufficient |
| macOS / Windows ARM media | operator-provided compatible FFmpeg | no pinned VEP binary or platform CI guarantee; run `doctor --media` before use |
| Caption gaps requiring ASR | Docker + pinned repository image + explicitly provisioned model | follow [ASR operations](asr-runtime.md); CPU works without an NVIDIA GPU, explicit CUDA remains fail-closed |

On Windows, the installer prints a process-local `VEP_FFMPEG_BIN_DIR` instruction.
Use that instruction in the shell running VEP, or use an already approved pair on
PATH. `-VerifyOnly` checks an existing pinned installation. Persistent selection
now requires `-SetUserEnvironment` or `-AddToUserPath`; an existing saved selection
is left unchanged. Older installer versions saved `VEP_FFMPEG_BIN_DIR` by default.
For an intentional upgrade, choose a process-local override or explicitly opt
into the new persistent selection. Do not add persistent environment settings as
a troubleshooting shortcut. On every OS, an FFmpeg build
missing `libx264`, PNG/PCM/WebVTT, required filters or decoder allocation limits
must fail diagnostics; do not bypass those checks. Follow the returned stable error
code and [media operations](cli-and-local-workspace.md), rather than adding arbitrary
FFmpeg options. ASR image smoke and GPU preflight are separate, explicit commands.

## Distribution and release boundary

The first distribution format is a commit-addressed **source bundle** plus checksum,
not an all-in-one executable. `python scripts/build_source_bundle.py --output dist`
requires a clean committed checkout and uses `git archive` of that exact commit.
It includes tracked repository resources and `LICENSE`, and records commit ID, archive SHA-256
and lock SHA-256 in `source-bundle.json` / `SHA256SUMS`. It contains no environment,
model, FFmpeg executable or job. Extract it to a new directory, verify the checksum,
then run the same sync/doctor commands. Git is needed for building, not for running
an extracted source bundle. Keep the bundle's commit manifest for identity.

Wheel/sdist builds remain development artifacts. A future standalone binary needs
an explicit resource-root contract, OS/architecture tests, signing, dependency and
license inventory, update/rollback design, and a measured reduction in operator cost.
Do not advertise it as portable simply because `vep --help` works.

VEP source is licensed under [AGPL-3.0-only](../../LICENSE). Keep `LICENSE` with
distributed source and package metadata. Third-party dependencies retain their own
terms; VEP's license does not grant rights to redistribute them under AGPL alone.
As of 2026-10-22, GitHub has no VEP Release, and the CI workflow has no deploy/publish
job. This work does not publish a package, tag a version or release binaries. Before
owner-approved release: separate package/contract versions, review dependency
notices, validate source bundle installation on each claimed OS, run the required
synthetic E2E and ASR build gate, and publish the reviewed checksums with release notes.
This is VEP's own CI; study-2026-fall `VEP_REQUIRED` is a downstream policy.

FFmpeg is obtained separately. `tools/ffmpeg-toolchain.json` identifies the Windows
Gyan 8.1.2 full build as GPL-3.0. The current VEP capability contract requires
`libx264`; [FFmpeg's own license explanation](https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/LICENSE.md)
describes how GPL components affect the FFmpeg build license. Check the exact
binary's `ffmpeg -L` / `-buildconf`, archive notices and corresponding source when
planning redistribution; generic “FFmpeg is LGPL” is insufficient. Do not copy
FFmpeg into a VEP bundle without that review. Obtain approved vendor packages from
the [Gyan build source](https://www.gyan.dev/ffmpeg/builds/) or the OS distributor.
Python packages, yt-dlp optional components, NVIDIA CUDA/cuDNN container layers and
ASR model weights each retain their own terms; the dependency lock or model revision
does not grant redistribution rights. Keep the current separate provisioning boundary.

## Update, rollback and migrate existing jobs

1. Finish or record pending exchanges; save the old commit, lock and ASR build contract.
   Back up the private workspace with its permissions outside the development Git repo.
2. Obtain the reviewed new revision in a separate checkout, sync its own venv, then
   run `doctor` (and `--media` when needed). Do not run `uv lock --upgrade` as an update.
3. Keep existing jobs under their original repository/workspace root initially.
   Run `vep job guide WORKSPACE --json` with the old installation and inspect the
   new revision's compatibility notes before switching a workspace in place. Resources
   are resolved relative to the jobs root; `--repository-root` on doctor does not
   relocate jobs or grant a resource-root override to other commands.
4. After a deliberate in-place source upgrade, run the new `job guide`. If it reports
   `migration-required`, use its transcript-boundary `job recover --child-spec` route.
   Keep the parent immutable; do not patch frozen requests, manifests or approval.
   Reuse verified transcript/source/normalization only through the public recovery
   contract. Recreate semantic evidence/author/audit for the child.
5. Keep the old checkout/venv and backup until review. Rollback means returning to
   the old checkout and an untouched compatible workspace or backup; it does not
   mean opening newly written artifacts with an older CLI without compatibility evidence.

No automatic self-update or workspace migration is performed. Changed ASR build
contracts require image rebuild, but valid model provisioning can be reused under
the existing verified profile/revision rules. No re-ASR is needed for transcript-boundary
recovery. See [getting started](../getting-started.md) and [workspace operations](cli-and-local-workspace.md).

## Performance evidence

Use `scripts/benchmark_perceptual.py` with a saved baseline `perceptual.py` and a JSON
output path. It generates deterministic PNG fixtures, alternates old/new fresh
processes serially, verifies exact pHash equality and records raw elapsed samples
and process peak RSS (including interpreter/imports). Record contention on a
shared host. This measures PNG/pHash only: it excludes FFmpeg decode, model load,
GPU VRAM and full pipeline execution. It cannot substantiate ASR or end-to-end gains.

2026-10-22 local comparison against main `6d1244616af34d3091828f47e156b4e4c595698a`,
Windows x64 / Python 3.12.13, three alternating fresh-process samples per implementation:

Fixtures are deterministic, non-interlaced 8-bit RGB (PNG color type 2), with one
IDAT stream compressed at zlib's default level. Channel bytes follow a repeating
arithmetic pattern, not photographic/noisy content. Every row uses the table's
single filter type. The 1920×1080 filter-0 case favors the direct-copy path; it is
not a representative video-frame mix or a worst-case filter-4 timing. Alpha,
palette and alternate bit depths were not benchmarked.

| RGB fixture | Median seconds, old → new | Median process peak RSS MiB, old → new |
| --- | --- | --- |
| 320×180, PNG filter 0 | 0.05719 → 0.00945 | 22.73 → 22.30 |
| 320×180, PNG filter 4 | 0.09840 → 0.07888 | 22.50 → 22.30 |
| 1920×1080, PNG filter 0 | 1.79460 → 0.01938 | 36.00 → 24.03 |

All samples produced identical pHashes. The larger fixture demonstrates reduced
PNG working memory after row-bounded inflation; the default-size RSS differences
are small and are not evidence of a material memory improvement. Results are
microbenchmarks on a shared LLM-01 host with other tasks potentially active; OS
load/power state were not isolated. No concurrent VEP build/test ran during timing.
Filter 4 still reconstructs every byte in Python. Quality settings, candidate
selection, timestamps and media output bytes are unchanged. CRC, decoded-length,
trailing-stream, filter and pixel-count checks remain mandatory.

The existing decoder contract remains non-interlaced 8-bit grayscale, RGB,
grayscale-alpha and RGBA (types 0/2/4/6). Alpha is ignored for luminance exactly
as before; this does not introduce alpha compositing. Palette, lower/16-bit depth
and interlaced PNG remain rejected. Regression tests cover all five filters,
mixed-filter rows, split IDAT, alpha variation, file/pixel limits and corrupt
headers/chunks/zlib streams. Unsupported formats are not newly advertised.

Before changing media quality, frame selection, beam size or model, use an agreed
local fixture and compare the same input, output and quality gates. Never send real
video/audio/assignments to external benchmark services.
