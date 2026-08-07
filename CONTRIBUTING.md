# Contribution guide

## Workflow

1. Issueで目的、SoT、受け入れ条件、非スコープを固定する。
2. `<actor>/<issue-number>-<scope>`形式のbranchを作成する。
3. 1 PR 1主要目的でDraft PRを作成する。
4. canonical文書またはSkillを変更した場合、台帳とevalを同じPRで更新する。
5. 文書台帳の`metadata_mode`に従い、registry-only文書へfront matterを追加しない。
6. CIと独立確認の結果をPR本文へ記録する。
7. Ready化・mergeはownerの明示判断後に行う。

## Local validation

```bash
uv sync --locked --all-groups
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run pytest -q
uv run vep schema check
uv run python scripts/validate_repository.py --root .
```

## Prohibited content

認証情報、署名付きURL、実動画、音声、字幕、抽出フレーム、実文字起こし、課題成果物を
Git、Issue、PR、Actions artifactへ追加しません。テストには合成か再配布可能なfixtureだけを
使用します。
