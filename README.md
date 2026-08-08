# Video Evidence Pipeline

認可済み動画を取得・正規化し、検証可能な証拠層を構築したうえで、レポート・要約等の生成、
独立監査、修正、人間承認、最終出力までを扱うローカル中心のパイプラインです。

> [!IMPORTANT]
> 現在実装済みなのは、core domain contracts、local workspace、`vep` CLI、明示authorization、
> `local_file`／`direct_http`／`generic_ytdlp`／`panopto`／`box`による
> `CREATED → AUTHORIZED → ACQUIRED`です。FFmpeg処理、ASR、
> evidence／authoring／audit／approval／exportは未実装です。

## Quickstart

Python 3.12と`uv`を用意し、認可済みのlocal sourceに対して次を実行します。

```bash
uv sync --locked --all-groups
uv run vep job init demo-report \
  --source-kind local_file \
  --source /path/to/authorized-source.mp4 \
  --task-type report \
  --language ja \
  --requirement "cite two concrete observations" \
  --requirement "separate facts from inference" \
  --output-format markdown
uv run vep job validate .local/jobs/demo-report
uv run vep job authorize .local/jobs/demo-report \
  --confirm \
  --basis "authorized course material" \
  --allow-local-processing \
  --deny-external-api
uv run vep acquire .local/jobs/demo-report
uv run vep job status .local/jobs/demo-report
```

`job init`だけでは未認可です。`job authorize --confirm`が成功するまでsourceを取得しません。
`--requirement`は順序を保持するrepeatable optionで、指定順を含めてjob fingerprintへ拘束されます。
raw sourceは`.local/`配下へcontent-addressedに保存され、上書きされません。

## Pipeline

1. authorization／requirements
2. acquisition
3. media normalization
4. evidence construction
5. deliverable authoring
6. independent audit
7. bounded revision
8. human approval
9. export

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
uv sync --locked --all-groups
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run pytest -q
uv run vep schema check
uv run python scripts/validate_repository.py --root .
```

## Data boundary

実講義動画、音声、字幕、Cookie、token、署名付きURL、文字起こし、ユーザー成果物はGit管理
しません。CIは実データや実credentialを使用しません。

## Status

- Implemented: core contracts、generated Schema、CLI、local workspace、local／network acquisition
- Current state path: `CREATED → AUTHORIZED → ACQUIRED`
- Next: FFmpeg／ffprobeによるmedia normalization（親ロードマップの後続Issue）
