---
schema_version: "1.0"
document_id: VEP-GOV-METADATA
title: "文書メタデータSchema"
document_type: governance
status: active
canonical: true
canonical_for: document-metadata-schema
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-GOV-DOCUMENTS", "VEP-GOV-CHANGE", "VEP-DOC-REGISTRY", "VEP-GOV-AI-INSTRUCTIONS"]
---
# 文書メタデータSchema

## 1. Scope

`docs/registry/documents.yaml`へ登録するMarkdown文書は、このSchemaのYAML front matterを
持ちます。Skillの`SKILL.md`、GitHub Issue/PR template、path-specific instructionsは、各機能の
専用Schemaを使用し、文書台帳の対象外です。

## 2. Required fields

- `schema_version`: 現在`"1.0"`
- `document_id`: リポジトリ内で一意、不変
- `title`
- `document_type`
- `status`: `draft`, `active`, `deprecated`, `historical`, `template`
- `canonical`
- `canonical_for`: canonical時に一意の責務slug
- `created_at`: 初回作成日時。不変
- `updated_at`: 最終の意味的変更日時
- `authors`
- `owners`
- `reviewers`
- `source_issue`
- `change_control`
- `supersedes`
- `superseded_by`
- `related_documents`

## 3. Semantics

`authors`は実質的な初稿・重要変更の作成主体、`owners`は維持責任者、`reviewers`は内容を
明示確認した主体です。AI自己監査やPR作成だけでreviewerを追加しません。

`canonical_for`はpathではなく責務を表し、同一値に複数canonical文書を認めません。
`created_at`は移動・履歴化後も変更せず、意味変更時だけ`updated_at`を更新します。

## 4. Historical conditions

historical文書は`canonical: false`、`status: historical`、`canonical_for: null`とし、
`historical_reason`、`historical_at`、`superseded_by` fieldを持ちます。置換先がある場合は文書ID、
責務廃止の場合はnullです。

## 5. Integrity rules

- `created_at <= updated_at`
- registry pathと実pathが一致
- front matterとregistryのID、type、status、canonical、canonical_for、ownersが一致
- `supersedes`、`superseded_by`、`related_documents`は登録済みIDを参照
- human review前に人間を`reviewers`へ追加しない

`scripts/validate_repository.py`とCIがこれらをfail closedで検証します。
