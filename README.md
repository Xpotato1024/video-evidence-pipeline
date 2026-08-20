# Video Evidence Pipeline

認可済み動画を取得・正規化し、検証可能な証拠層を構築したうえで、レポート・要約等の生成、
独立監査、修正、人間承認、最終出力までを扱うローカル中心のパイプラインです。

> [!IMPORTANT]
> 現在実装済みなのは、core domain contracts、local workspace、`vep` CLI、明示authorization、
> `local_file`／`direct_http`／`generic_ytdlp`／`panopto`／`box`による
> acquisition、video／audio／supplied transcript input、system FFmpeg／ffprobeによるmedia normalization、caption-first transcription、
> provider-neutralなevidence／authoring／independent-audit／revision prepare／import、private SQLite FTS5
> search、人間確認package、明示的人間approval、byte-exact Markdown exportです。直線経路は
> `CREATED → AUTHORIZED → ACQUIRED → NORMALIZED → TRANSCRIBED → TRANSCRIPT_REFINED → EVIDENCE_READY → DRAFTED → AUDITED → HUMAN_REVIEW_READY → HUMAN_APPROVED → EXPORTED`、
> material findingがある経路は最大2回の`REVISION_REQUIRED → DRAFTED → AUDITED`を経て、未解消なら
> `BLOCKED`です。既存v1 video jobは`TRANSCRIBED → EVIDENCE_READY`を維持します。DOCX／PDF／HTML exportは未実装です。

## Quickstart

### Pinned FFmpeg/ffprobe (Windows)

Media normalization requires both `ffmpeg` and `ffprobe`. Install the pinned
Windows build and verify its archive and executable SHA-256 values with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_ffmpeg.ps1
```

The version, download URL, archive hash, executable hashes, and the
`VEP_FFMPEG_BIN_DIR` selection are maintained in
[`tools/ffmpeg-toolchain.json`](tools/ffmpeg-toolchain.json). Use
`-VerifyOnly` to re-check an existing installation without downloading or
changing user environment variables. The installer sets
`VEP_FFMPEG_BIN_DIR` by default; pass `-AddToUserPath` only when command-line
`ffmpeg`／`ffprobe` access through User PATH is required.

Python 3.12と`uv`を用意し、認可済みのlocal sourceに対して次を実行します。

```bash
uv sync --locked
uv run vep job init demo-report \
  --source-kind local_file \
  --source /path/to/authorized-source.mp4 \
  --primary-input video \
  --task-type report \
  --language ja \
  --requirement "cite two concrete observations" \
  --requirement "separate facts from inference" \
  --max-chars 300 \
  --output-format markdown
uv run vep job validate .local/jobs/demo-report
uv run vep job authorize .local/jobs/demo-report \
  --confirm \
  --basis "authorized course material" \
  --allow-local-processing \
  --deny-external-api
uv run vep acquire .local/jobs/demo-report
uv run vep normalize .local/jobs/demo-report
# ASRが必要な場合: build contract変更時はimageを再buildし、同じmodel revisionは再利用する
uv run vep asr image-build --profile whisper-large-v3
uv run vep asr model-provision --profile whisper-large-v3
uv run vep transcribe .local/jobs/demo-report --preferred-language ja \
  --asr-profile whisper-large-v3 --asr-device auto
uv run vep refine prepare .local/jobs/demo-report
# refinement/request.jsonを固定入力として、認可済みresponse.jsonを別途用意する
uv run vep refine import .local/jobs/demo-report --response /path/to/refinement-response.json
uv run vep evidence prepare .local/jobs/demo-report
# evidence/request.jsonを固定入力として、認可済みのprovider-neutralなresponse.jsonを別途用意する
uv run vep evidence import .local/jobs/demo-report --response /path/to/response.json
uv run vep index build .local/jobs/demo-report
uv run vep search .local/jobs/demo-report "検索語" --limit 20
# byte-level integrity investigationが必要な場合だけ大容量artifactも再hashする
uv run vep job validate .local/jobs/demo-report --deep
# author／audit（必要ならrevision）のvalidated exchange後、表示されたtarget SHAを人間が確認する
uv run vep approve .local/jobs/demo-report \
  --approval-target-sha256 <reviewed-draft-sha256> \
  --reviewer "local-reviewer" \
  --basis "reviewed the human-review package" \
  --confirm
uv run vep export .local/jobs/demo-report
uv run vep job status .local/jobs/demo-report
```

`job init`だけでは未認可です。`job authorize --confirm`が成功するまでsourceを取得しません。
`--requirement`は順序を保持するrepeatable optionで、指定順を含めてjob fingerprintへ拘束されます。
`--min-chars`／`--max-chars`はmachine-readableなassignment constraintです。maxだけなら
`ceil(max × 0.8)`をeffective minにし、Pythonのcanonical counterが各draft／revision／review／export境界で検証します。
raw sourceは`.local/`配下へcontent-addressedに保存され、上書きされません。
ASR engine／runtime／model／revision／device policyは[`config/asr-profiles.yaml`](config/asr-profiles.yaml)が正本です。
host pipelineがcaption selectionとgap計算を所有し、不足区間だけをtyped ASR worker containerへ渡します。
containerへcanonical job workspaceはmountせず、exact audioと`.local/models`のprovision済みmodelだけをread-onlyで
渡し、`--network none`で実行します。captionが十分ならDockerもmodelも不要です。不足区間でmodelが未provisionなら
暗黙downloadせず停止します。外部APIを使った
evidence responseのimportにはjobの`allow_external_api`認可が必要です。
新規CLI jobは`--primary-input`を省略してもmodern videoとして作成されます。
`--primary-input video|audio|transcript`と、必要な場合だけrepeatableな
`--supporting-input pdf:PATH|image:PATH|notes:PATH`を指定できます。新input宣言を持つjobはtranscribe後に
`vep refine prepare`／`vep refine import`を実行し、raw transcriptとrefined transcriptを分離します。
authoring／audit／revisionの`external_exchange` public production importは、対応するrepository-local Skillが
`active`になっている場合だけ利用できます。現行のactive Skillは`draft-video-report`、`draft-video-summary`、
`independent-content-audit`、`revise-from-audit`で、いずれも明示選択専用（`automatic_trigger = false`）かつ
`instruction-only`です。`analyze-video-evidence`と`prepare-human-review`はdraftのままで、このproduction
routeには使用しません。allowlist内のSkillがdraftならimportは`DRAFT_SKILL_FORBIDDEN`でfail closedします。
synthetic fixture実行は内部test routeだけです。
`vep approve`はAI／Skill実行ではなく、対象SHA、reviewer、basis、`--confirm`を明示する人間操作です。
reviewer identityはlocal CLIの自己申告lineageであり、暗号学的な本人確認ではありません。

## Transcript-boundary recovery

`vep job recover` accepts a readable parent with verified source, normalization,
raw-transcript, and supporting-input artifacts. This includes successful jobs from
`TRANSCRIBED` through `EXPORTED`, plus `BLOCKED` jobs that previously reached the
transcript boundary. Pre-transcript parents are rejected. Recovery does not require a
finding category, an old draft/audit fingerprint, or any other semantic history to match.

```bash
uv run vep job recover .local/jobs/completed-report changed-assignment-report \
  --child-spec /path/to/repaired-job.yaml \
  --confirm \
  --basis "rerun the verified transcript under a changed assignment"
```

The parent workspace remains untouched. The child has a new job-bound authorization
record with the same permission flags. `--child-spec` may replace assignment and output
constraints while source／primary／supporting input identity must match. Recovery reuses only
source/normalization/transcript and declared supporting-input artifacts, starts at `TRANSCRIBED`,
and records both assignment fingerprints. Run refinement,
evidence, authoring, and audit again as fresh child work. Existing network acquisition
evidence (including Box) is rebound and verified locally; recovery never re-acquires it.
See [CLI and local workspace operations](docs/operations/cli-and-local-workspace.md) and
[ASR runtime operations](docs/operations/asr-runtime.md).

## Supported sources

The matrix below describes the current VEP source contract. `Supported` means
the source has a bounded VEP adapter; `Partial/site-dependent` means that
provider or extractor behavior can still limit acquisition.

| Source | source-kind | Input form | Status | Authentication | Backend | Limitations |
|---|---|---|---|---|---|---|
| Local file | `local_file` | Local filesystem path | Supported | Local processing authorization | Local immutable copy | The caller must have access to the local file. |
| Direct HTTPS media | `direct_http` | HTTPS direct media URL | Supported | Local processing and network acquisition authorization | Pinned HTTPX transport | HTTPS direct media only. |
| YouTube | `generic_ytdlp` | YouTube URL | Partial/site-dependent | Optional explicit browser cookies | Hardened yt-dlp | Current yt-dlp behavior applies. PO Token and other cases outside the VEP contract are not guaranteed. |
| Other yt-dlp-supported services | `generic_ytdlp` | One service URL | Partial/site-dependent | Explicit browser cookies only where the existing policy allows them | Hardened yt-dlp | No playlists or arbitrary arguments; the contract is one progressive A/V source. |
| Panopto | `panopto` | Viewer URL or canonical session locator | Supported | Official REST token first; explicit browser-cookie yt-dlp fallback only under the existing policy | Panopto REST with constrained yt-dlp fallback | Tenant policy, download prohibition, and DRM remain fail-closed. |
| Box file ID/file URL | `box` | Numeric ID, `box://file/<id>`, `https://app.box.com/files/<id>`, or `https://app.box.com/file/<id>` | Supported | Official Box API token via `--box-token-env` | Box Files metadata/content API | File-level acquisition only. |
| Box shared link | `box` | `https://app.box.com/s/<token>` or `https://www.box.com/s/<token>` | Supported | Official Box API token via `--box-token-env` | Box `shared_items` plus Files API | The shared item must resolve to a file; shared-link passwords are unsupported. |
| Box Enterprise shared link | `box` | `https://<enterprise>.ent.box.com/s/<token>` | Supported | Official Box API token via `--box-token-env` | Box `shared_items` plus Files API | Uses official shared-item resolution; no EntBox adapter/source kind is introduced. |
| Box folder shared link | `box` | Box folder shared link | Unsupported | — | — | Folder download is not a video acquisition contract. |
| DRM/access-control bypass | — | — | Unsupported | — | — | VEP does not bypass provider restrictions. |

Box shared links are capability-like private inputs. When a job needs one, the
full link is kept only in the private local job input; manifests, acquisition
evidence, stage manifests, and logs use redacted identity such as a locator
fingerprint. A browser login alone does not satisfy the official Box REST token
route; without an access token, shared-link acquisition is blocked.

## Pipeline

1. authorization／requirements
2. acquisition
3. media normalization
4. caption／ASR／supplied transcriptからのraw transcript
5. transcript refinement（新規CLI job。保存済みv1 jobはlegacy routeを維持）
6. evidence construction／ambiguity preservation
7. private rebuildable search cache
8. deliverable authoring＋deterministic length gate
9. independent audit
10. bounded revision
11. human approval
12. export

## SoT

設計索引は[`docs/README.md`](docs/README.md)、文書台帳は
[`docs/registry/documents.yaml`](docs/registry/documents.yaml)です。

- 共通AIポリシー: [`.agents/policy.md`](.agents/policy.md)
- Skillガバナンス: [`.agents/governance.md`](.agents/governance.md)
- Skill台帳: [`.agents/skill-system.toml`](.agents/skill-system.toml)
- 文書メタデータ: [`docs/governance/metadata-schema.md`](docs/governance/metadata-schema.md)
- CI品質ゲート: [`docs/operations/ci-quality-gates.md`](docs/operations/ci-quality-gates.md)
- CLI／workspace運用: [`docs/operations/cli-and-local-workspace.md`](docs/operations/cli-and-local-workspace.md)
- Network取得運用: [`docs/operations/network-acquisition.md`](docs/operations/network-acquisition.md)
- contribution: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- security: [`SECURITY.md`](SECURITY.md)

READMEやAI入口文書の識別情報は文書台帳だけで管理し、本文先頭へ汎用YAML front matterを
重複させません。

## AI entrypoints

各AIの自動検出位置は維持し、共通正本へ接続します。

- `AGENTS.md`
- `CLAUDE.md`
- `GEMINI.md`
- `.github/copilot-instructions.md`

## Validation

```bash
uv sync --locked
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run pytest -q
uv run vep schema check
uv run python scripts/validate_repository.py --root .
```

## Data boundary

実講義動画、音声、字幕、Cookie、token、署名付きURL、文字起こし、evidence response／ledger、
SQLite index、ユーザー成果物はGit管理しません。runtime artifactとindexはprivate workspace内だけに置き、
CIは実データや実credentialを使用しません。

## Status

- Implemented: core contracts、generated Schema、CLI、local workspace、local／network acquisition、video／audio／transcript input、transcript refinement、PDF／image／notes supporting input、evidence ledger、private FTS5 cache、bounded authoring／independent audit／revision、人間確認package、hash-bound human approval、byte-exact Markdown export
- Current state path: `… → EVIDENCE_READY → DRAFTED → AUDITED → HUMAN_REVIEW_READY → HUMAN_APPROVED → EXPORTED`、または最大2回のrevision後に`BLOCKED`
- Next: ownerによるstacked Draft PRの監査とrootから順のmerge
