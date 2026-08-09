# Video Evidence Pipeline

認可済み動画を取得・正規化し、検証可能な証拠層を構築したうえで、レポート・要約等の生成、
独立監査、修正、人間承認、最終出力までを扱うローカル中心のパイプラインです。

> [!IMPORTANT]
> 現在実装済みなのは、core domain contracts、local workspace、`vep` CLI、明示authorization、
> `local_file`／`direct_http`／`generic_ytdlp`／`panopto`／`box`による
> acquisition、system FFmpeg／ffprobeによるmedia normalization、caption-first transcription、
> provider-neutralなevidence／authoring／independent-audit／revision prepare／import、private SQLite FTS5
> search、人間確認package作成です。直線経路は
> `CREATED → AUTHORIZED → ACQUIRED → NORMALIZED → TRANSCRIBED → EVIDENCE_READY → DRAFTED → AUDITED → HUMAN_REVIEW_READY`、
> material findingがある経路は最大2回の`REVISION_REQUIRED → DRAFTED → AUDITED`を経て、未解消なら
> `BLOCKED`です。human approval／exportは未実装です。

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
uv run vep normalize .local/jobs/demo-report
uv run vep transcribe .local/jobs/demo-report --preferred-language ja
uv run vep evidence prepare .local/jobs/demo-report
# evidence/request.jsonを固定入力として外部analystがresponse.jsonを作成する
uv run vep evidence import .local/jobs/demo-report --response /path/to/response.json
uv run vep index build .local/jobs/demo-report
uv run vep search .local/jobs/demo-report "検索語" --limit 20
uv run vep job status .local/jobs/demo-report
```

`job init`だけでは未認可です。`job authorize --confirm`が成功するまでsourceを取得しません。
`--requirement`は順序を保持するrepeatable optionで、指定順を含めてjob fingerprintへ拘束されます。
raw sourceは`.local/`配下へcontent-addressedに保存され、上書きされません。
captionが十分ならASR package／modelは不要です。不足区間だけASRを使用する場合も、明示された完全な
local model directoryをofflineで使用し、暗黙downloadやcloud送信を行いません。外部APIを使った
evidence responseのimportにはjobの`allow_external_api`認可が必要です。
authoring／audit／revisionのproduction importは、対応するrepository-local Skillが`active`になっている場合だけ
利用できます。現行の関連Skillは`draft`のため、request作成はできますがproduction importは
`DRAFT_SKILL_FORBIDDEN`でfail closedします。synthetic fixture実行は内部test routeだけです。

## Pipeline

1. authorization／requirements
2. acquisition
3. media normalization
4. caption-first transcription
5. evidence construction／ambiguity preservation
6. private rebuildable search cache
7. deliverable authoring
8. independent audit
9. bounded revision
10. human approval
11. export

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

実講義動画、音声、字幕、Cookie、token、署名付きURL、文字起こし、evidence response／ledger、
SQLite index、ユーザー成果物はGit管理しません。runtime artifactとindexはprivate workspace内だけに置き、
CIは実データや実credentialを使用しません。

## Status

- Implemented: core contracts、generated Schema、CLI、local workspace、local／network acquisition、media normalization、transcription、evidence ledger、private FTS5 cache、bounded authoring／independent audit／revision、人間確認package
- Current state path: `… → EVIDENCE_READY → DRAFTED → AUDITED → HUMAN_REVIEW_READY`、または最大2回のrevision後に`BLOCKED`
- Next: hash-bound human approval／export（親ロードマップの後続Issue）
