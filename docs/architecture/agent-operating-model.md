---
schema_version: "1.0"
document_id: VEP-ARCH-AGENTS
title: "エージェント運用モデル"
document_type: architecture
status: draft
canonical: true
canonical_for: agent-operating-model
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:07:00+09:00
authors:
  - Xpotato1024
  - OpenAI ChatGPT
owners:
  - Xpotato1024
reviewers: []
source_issue: 1
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENTS", "VEP-ARCH-PIPELINE", "VEP-ARCH-STATE", "VEP-ARCH-ARTIFACTS"]
---
# エージェント運用モデル

## 1. 目的

機械的処理、AIによる意味処理、人間判断の責務を明確にし、ハルシネーション、誤読、自己監査の形骸化を抑制します。

## 2. 実行主体

### Deterministic executor

- source probe／download
- hash、ffprobe、FFmpeg
- subtitle conversion
- scene/frame extraction
- Schema validation
- index
- length／format check
- bundle作成
- approval hash check

同じ入力fingerprintに対して再現可能であることを目標とします。

### Evidence analyst

- transcript修正候補
- frame observation
- topic segmentation
- evidence record
- ambiguity extraction

レポート本文を生成しません。

### Deliverable author

- assignment requirement map
- outline
- draft
- author claim ledger

証拠不足を自然な文章で補完しません。

### Independent auditor

- fresh contextでdraft claimを再抽出
- evidenceとの照合
- requirement compliance
- P0／P1／P2 findings

本文を修正しません。

### Reviser

- findingsのresolution
- evidenceに基づく修正
- revised claim ledger
- resolution ledger

監査所見にない全面改稿を既定動作にしません。

### Human-review preparer

- 再生確認が必要なtimestamp
- low-confidence claim
- user opinion未確定箇所
- 変更差分
- approval checklist

### Human approver

ユーザー本人です。AIへ委譲できません。

## 3. Skill boundary

予定Skill:

- `analyze-video-evidence`
- `draft-video-report`
- `draft-video-summary`
- `independent-content-audit`
- `revise-from-audit`
- `prepare-human-review`

Skillは指示と参照資料を提供しますが、media processingやSchema validationの決定論的ロジックを重複実装しません。

## 4. Authoring contract

入力:

- validated assignment requirements
- validated evidence bundle
- user notes
- output constraints

出力:

- `draft.md`
- `claims.jsonl`
- `requirement-map.json`
- `authoring-manifest.json`

禁止:

- evidence bundle外のvideo fact
- user opinionの捏造
- source timestampの創作
- unresolved ambiguityの無印断定
- external source禁止時の外部情報追加

## 5. Audit contract

入力:

- assignment
- source／media manifest
- transcript／frame evidence
- target draft
- audit Schema

author prompt history、private reasoning、revision intentionは渡しません。

出力:

- `audit-claims.jsonl`
- `findings.json`
- `audit-report.md`
- `audit-manifest.json`

severity:

- `P0`: 虚偽、逆内容、設問不成立、出典偽装、重大要件欠落
- `P1`: 重要根拠不足、過剰推論、誤読、timestamp不一致、形式違反
- `P2`: 表現、冗長性、軽微な構成・明確性

## 6. Independence

最低条件:

- 新しい会話／agent execution
- audit bundle固定
- authorのchain of thoughtを不使用
- author claim ledgerを補助扱い
- source evidenceへ直接アクセス
- audit結果を構造化

同じmodelを使うこと自体は禁止しませんが、独立性が弱くなる可能性をmanifestに記録します。将来はmodel diversityをoptionとして評価します。

## 7. Revision loop

```text
draft v1
  → audit
  → P0/P1あり
  → revision v2
  → fresh audit
  → P0/P1あり
  → revision v3
  → fresh audit
  → still P0/P1
  → BLOCKED / human escalation
```

既定の自動revision上限は2回です。

## 8. Human review package

最低限:

- draft
- previous draftとの差分
- audit summary
- unresolved P2
- timestamp付き確認対象
- user opinion箇所
- requirement map
- approval対象SHA-256

## 9. 監査可能性

各AI実行は次を記録します。

- role
- Skill ID/version
- model/provider
- execution context ID
- input bundle hash
- output hash
- started/finished time
- external API permission
- warnings

private reasoningの保存を要件にしません。再現・監査に必要なのは入力、明示指示、構造化出力、版本です。
