---
schema_version: "1.0"
document_id: VEP-DOCS-INDEX
title: "設計ドキュメント索引"
document_type: documentation-index
status: active
canonical: true
canonical_for: documentation-index
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
related_documents: ["VEP-GOV-DOCUMENTS", "VEP-DOC-REGISTRY", "VEP-README"]
---
# 設計ドキュメント索引

## 1. この索引の責務

この文書は、現在有効な設計SoTへの入口を提供します。各設計の詳細を再記述せず、責務、正本、成熟度、依存関係を示します。

機械可読な正本一覧は [`registry/documents.yaml`](registry/documents.yaml) です。索引と台帳が矛盾した場合は変更を停止し、同一PRで整合させてください。

## 2. 文書ガバナンス

| 文書 | 責務 |
|---|---|
| [`governance/document-governance.md`](governance/document-governance.md) | canonical／historical、正本性、文書分類 |
| [`governance/metadata-schema.md`](governance/metadata-schema.md) | YAML front matterのfieldと制約 |
| [`governance/change-control.md`](governance/change-control.md) | 作成、更新、置換、履歴化、廃止の手順 |

## 3. アーキテクチャ

| 文書 | 責務 |
|---|---|
| [`architecture/system-context.md`](architecture/system-context.md) | 利用者、外部システム、対象範囲 |
| [`architecture/pipeline-architecture.md`](architecture/pipeline-architecture.md) | パイプライン層と処理境界 |
| [`architecture/artifact-model.md`](architecture/artifact-model.md) | 成果物、lineage、immutable boundary |
| [`architecture/state-machine.md`](architecture/state-machine.md) | ジョブ状態と遷移条件 |
| [`architecture/trust-boundaries.md`](architecture/trust-boundaries.md) | 認証、外部送信、Git境界、脅威 |
| [`architecture/technology-baseline.md`](architecture/technology-baseline.md) | 採用・任意・保留技術 |
| [`architecture/agent-operating-model.md`](architecture/agent-operating-model.md) | 機械、AI、人間の役割と独立監査 |

## 4. 設計判断

- [`decisions/README.md`](decisions/README.md): ADR運用と一覧
- [`decisions/0000-template.md`](decisions/0000-template.md): ADRテンプレート

ADRは判断理由を保存します。現在の設計仕様はarchitecture／governance文書を正本とし、ADR単独で現行仕様を定義しません。

## 5. Historical archive

[`historical/README.md`](historical/README.md) は、重要な旧設計を保存するための規約と索引です。historical文書は現在の実装判断に直接使用してはいけません。

## 6. テンプレート

- [`templates/canonical-document.md`](templates/canonical-document.md)
- [`templates/historical-document.md`](templates/historical-document.md)

## 7. 成熟度

現時点の文書は、実装開始前の設計基盤です。`status: active` は文書運用上の正本であることを示しますが、実装済み・動作確認済みを意味しません。実装との整合性は後続Issueで検証します。
