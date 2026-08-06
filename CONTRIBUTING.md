---
schema_version: "1.0"
document_id: VEP-CONTRIBUTING
title: "Contribution guide"
document_type: governance
status: active
canonical: true
canonical_for: contribution-workflow
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-POLICY", "VEP-GOV-CHANGE", "VEP-OPS-CI"]
---
# Contribution guide

## Workflow

1. Issueで目的、SoT、受け入れ条件、非スコープを固定する。
2. `<actor>/<issue-number>-<scope>`形式のbranchを作成する。
3. 1 PR 1主要目的でDraft PRを作成する。
4. canonical文書またはSkillを変更した場合、台帳とevalを同じPRで更新する。
5. CIと独立確認の結果をPR本文へ記録する。
6. Ready化・mergeはownerの明示判断後に行う。

## Local validation

```bash
uv sync --locked --all-groups
uv run ruff check scripts tests
uv run ruff format --check scripts tests
uv run pytest -q
uv run python scripts/validate_repository.py --root .
```

## Prohibited content

認証情報、署名付きURL、実動画、音声、字幕、抽出フレーム、実文字起こし、課題成果物を
Git、Issue、PR、Actions artifactへ追加しません。テストには合成か再配布可能なfixtureだけを
使用します。
