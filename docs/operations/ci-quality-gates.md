---
schema_version: "1.1"
document_id: VEP-OPS-CI
title: "CI・品質ゲート運用"
document_type: operations
status: active
canonical: true
canonical_for: ci-quality-gates
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-10T02:00:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-ARCH-TECH", "VEP-AGENT-GOV", "VEP-CONTRIBUTING", "VEP-SECURITY", "VEP-OPS-NETWORK"]
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

`lint`はRuffのlint／format、`tests`はUbuntu package repositoryからsystem FFmpeg／ffprobeを明示導入し、
capability確認とsynthetic mediaだけを使う実変換integrationを必須化し、
`repository-validation`は実リポジトリ全体の次を検査します。

- 文書front matterと文書台帳
- `document_id`と`canonical_for`の一意性
- 文書間参照とrelative link
- Skill registry、SKILL.md、eval定義／result、lifecycle
- active Skillのcurrent passing result evidence（Skill SHA、eval SHA、評価対象commit、P0／P1、owner review）
- vendor adapter marker
- YAML／TOML syntax
- workflow Actionの完全commit SHA pin
- 高確度secret pattern
- 実メディア、ローカルjob、ユーザー成果物の誤コミット
- installable packageと`vep` entry point
- Pydantic modelから生成したcommit済みJSON Schemaのdrift
- `src/`を含むRuff対象とCI command

## 4. 実行環境

CIはGitHub-hosted runnerで合成・公開可能なfixtureのみを扱います。Panopto、Box、ブラウザ
Cookie、OpenAI等のcredentialをActions secretsへ登録しません。

network acquisition testはHTTPX MockTransport、fake DNS resolver、fake subprocess runnerを使用し、
real networkへ接続しません。synthetic token／Cookie／signed URL markerがmanifest、CLI JSON、transition、
log、safe exceptionへ残らないことも検査します。manual live integrationは通常CIの成功条件にしません。

media integrationは`VEP_REQUIRE_FFMPEG_TESTS=1`でtool不在時のskipを禁止します。実講義mediaや外部取得を
使わず、CI内で生成した短いvideo／audio／subtitle fixtureについてproxy、speech audio、subtitle、frame、
manifest、transition、idempotent reuseを検証します。localでtoolがPATHにない場合だけ同testは理由付きskipです。
同じsynthetic fixtureからcaption-only transcription、provider-neutral fixture responseの明示的test hook、
`TRANSCRIBED → EVIDENCE_READY`、FTS5 trigramの日本語substring／rebuild／tamper／stale検出まで検証します。
ASR package／model／network downloadはCIへ要求せず、ASR経路はinjected fake backendだけで検証します。
`tests/test_full_e2e.py`は同じsynthetic dataからstraight pathと1回revision pathを`EXPORTED`まで実行し、
明示的synthetic human action、approval SHA、byte-exact Markdown、全transition、idempotencyをrequired stepとして
検証します。approval／exportのfocused testsはtamper、crash recovery、concurrencyを別に検証します。
実provider、credential、実講義mediaは使用しません。

ActionsはNode 24対応版を完全commit SHAでpinし、Dependabotが更新候補を作成します。

## 5. Local parity

```bash
uv sync --locked --all-groups
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run pytest -q
uv run vep schema check
uv run python scripts/validate_repository.py --root .
```

## 6. Fail closed

台帳競合、Skill契約不整合、secret疑い、禁止artifact、未pin Actionを検出した場合、警告では
なく失敗とします。例外が必要な場合はvalidatorのallowlistとcanonical文書を同じPRで変更し、
理由を残します。
