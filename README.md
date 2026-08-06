# Video Evidence Pipeline

認可済み動画を取得・正規化し、検証可能な証拠層を構築したうえで、レポート・要約等の生成、
独立監査、修正、人間承認、最終出力までを扱うローカル中心のパイプラインです。

> [!IMPORTANT]
> 設計、AI運用、Skills契約、repository validation、CIの基盤は整備済みです。動画取得、
> FFmpeg処理、ASR、evidence／claim／audit domainの実装はまだ存在しません。

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
uv run ruff check scripts tests
uv run ruff format --check scripts tests
uv run pytest -q
uv run python scripts/validate_repository.py --root .
```

## Data boundary

実講義動画、音声、字幕、Cookie、token、署名付きURL、文字起こし、ユーザー成果物はGit管理
しません。CIは実データや実credentialを使用しません。

## Status

- Foundation: design SoT、agent governance、Skills contracts、CI／quality gates
- Next: deterministic domain implementation
