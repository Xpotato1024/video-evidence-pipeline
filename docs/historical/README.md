---
schema_version: "1.0"
document_id: VEP-HISTORICAL-INDEX
title: "Historical文書索引"
document_type: historical-index
status: active
canonical: true
canonical_for: historical-document-index
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:20:44+09:00
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
related_documents: ["VEP-GOV-DOCUMENTS", "VEP-GOV-CHANGE", "VEP-TEMPLATE-HISTORICAL"]
---
# Historical文書索引

このディレクトリは、過去に有効だった重要な設計状態を保存します。ここにある文書は現在の正本ではありません。

> [!WARNING]
> historical文書を現在の実装判断やAI作業指示へ直接使用しないでください。現在の正本は`docs/registry/documents.yaml`から確認してください。

## 配置

```text
docs/historical/<YYYY-MM-DD>/<元のdocs配下path>
```

例:

```text
docs/historical/2026-10-01/architecture/pipeline-architecture.md
```

## 保存対象

- 互換性を破壊する設計変更前のarchitecture
- 旧state machine
- 旧trust boundary
- 移行前のartifact contract
- 重大incident時点の設計snapshot

誤字やformat変更は保存対象にしません。

## 必須メタデータ

- `canonical: false`
- `canonical_for: null`
- `status: historical`
- `historical_at`
- `historical_reason`
- `superseded_by`（置換先がある場合は文書ID、責務廃止の場合はnull）

## 一覧

現在、historical文書はありません。
