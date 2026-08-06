---
schema_version: "1.0"
document_id: VEP-OPS-CI
title: "CI・品質ゲート運用"
document_type: operations
status: active
canonical: true
canonical_for: ci-quality-gates
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-06T17:05:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-ARCH-TECH", "VEP-AGENT-GOV", "VEP-CONTRIBUTING", "VEP-SECURITY"]
---
# CI・品質ゲート運用

## 1. Implemented checks

GitHub Actionsで次のcheckを実装し、PRと`main`へのpushで実行します。

- `lint`
- `tests`
- `repository-validation`

全jobは`ubuntu-24.04`、Python 3.12、固定したuv version、commit済み`uv.lock`を使用し、
`GITHUB_TOKEN`は`contents: read`だけを持ちます。

## 2. Branch enforcement status

上記checkを`main`のrequired status checksへ登録することが目標状態です。しかし、2026-08-06時点で
個人所有Privateリポジトリのruleset APIは、GitHub ProへのupgradeまたはPublic化が必要として
拒否されます。Private維持を優先し、現在は次を手動gateとします。

- checkがすべて成功するまでReady化・mergeしない
- PR本文へ実行結果と未実行項目を記録する
- ownerの明示判断なしにmergeしない

GitHubプランが要件を満たした時点で、`lint`、`tests`、`repository-validation`をrequired checksへ
登録し、PR必須、conversation resolution、linear history、force-push／deletion禁止を有効化します。

## 3. 検査内容

`lint`はRuffのlint／format、`tests`はvalidatorのunit/contract test、
`repository-validation`は実リポジトリ全体の次を検査します。

- 文書front matterと文書台帳
- `document_id`と`canonical_for`の一意性
- 文書間参照とrelative link
- Skill registry、SKILL.md、eval、lifecycle
- vendor adapter marker
- YAML／TOML syntax
- workflow Actionの完全commit SHA pin
- 高確度secret pattern
- 実メディア、ローカルjob、ユーザー成果物の誤コミット

## 4. 実行環境

CIはGitHub-hosted runnerで合成・公開可能なfixtureのみを扱います。Panopto、Box、ブラウザ
Cookie、OpenAI等のcredentialをActions secretsへ登録しません。

ActionsはNode 24対応版を完全commit SHAでpinし、Dependabotが更新候補を作成します。

## 5. Local parity

```bash
uv sync --locked --all-groups
uv run ruff check scripts tests
uv run ruff format --check scripts tests
uv run pytest -q
uv run python scripts/validate_repository.py --root .
```

## 6. Fail closed

台帳競合、Skill契約不整合、secret疑い、禁止artifact、未pin Actionを検出した場合、警告では
なく失敗とします。例外が必要な場合はvalidatorのallowlistとcanonical文書を同じPRで変更し、
理由を残します。
