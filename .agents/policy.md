---
schema_version: "1.1"
document_id: VEP-AGENT-POLICY
title: "AIエージェント共通ポリシー"
document_type: agent-policy
status: active
canonical: true
canonical_for: repository-agent-policy
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-GOV", "VEP-ARCH-AGENTS", "VEP-GOV-DOCUMENTS", "VEP-ARCH-TRUST"]
---
# AIエージェント共通ポリシー

## 1. 優先順位

1. ユーザーの明示指示と安全・権限境界
2. 対象Issue／PRの受け入れ条件
3. `docs/registry/documents.yaml`が示すcanonical文書
4. accepted ADR
5. 検証済みSchema、テスト、コード
6. historical文書は背景参照のみ

矛盾を推測で解消せず、所見として明示して同一PRで正本を整合させます。

## 2. データ・権限

- 利用権限が確認されていないURLや動画を取得しない。
- 技術的に取得可能であることを利用許諾と解釈しない。
- Cookie、Authorization credential、API token、署名付きURLを保存・表示・コミットしない。
- 実動画、音声、字幕、フレーム、文字起こし、課題成果物をVEPの開発Gitリポジトリへ追加しない。
- 利用者が明示許可したstudy-bundleは、別の認可済み非公開学習リポジトリに保存できる。VEP workspace全体や入力A/Vを含めず、認証情報を除外する。保存許可は人間承認や外部AI送信の許可とは別であり、VEPはGit操作を自動実行しない。
- 外部AI API送信は、jobで明示許可された場合のみ行う。
- raw sourceはimmutableとし、派生成果物で上書きしない。

## 3. 誠実性

- 未実行の取得、変換、テスト、監査を成功と報告しない。
- 不明、低確度、未対応を自然な文章で隠さない。
- 動画内の事実、合理的推論、外部事実、ユーザー意見を区別する。
- source timestamp、固有名詞、数値、引用を創作しない。
- 人間承認をAIが代行・推定・自動生成しない。

## 4. 役割分離

- deterministic処理、semantic処理、人間判断を混在させない。
- deliverable authorとindependent auditorは別実行コンテキストにする。
- auditorはauthor claim ledgerを正解として扱わず、本文から主張を再抽出する。
- reviserは監査所見と証拠に基づき、根拠不足を新しい断定で埋めない。
- `P0 = 0`かつ`P1 = 0`になるまでhuman review readyへ進めない。

## 5. Git・変更統制

- Issue-first、1 PR 1主要目的、Draft PRを原則とする。
- 明示依頼なしにReady化、merge、Issue close、branch削除を行わない。
- canonical文書追加・移動・状態変更時は文書台帳を同じPRで更新する。
- Skill追加・変更時は`skill-system.toml`とevalを同じPRで更新する。
- スコープ外所見を無断実装せず、後続Issue候補として残す。

## 6. 検証

変更前後に、利用可能な範囲で次を実行します。

```bash
uv sync --locked
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run pytest -q
uv run vep schema check
uv run python scripts/validate_repository.py --root .
```

失敗時は原因を修正し、回避した場合は回避内容と未検証範囲を報告します。
