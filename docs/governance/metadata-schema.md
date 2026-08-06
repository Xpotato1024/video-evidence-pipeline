---
schema_version: "1.1"
document_id: VEP-GOV-METADATA
title: "文書メタデータSchema"
document_type: governance
status: active
canonical: true
canonical_for: document-metadata-schema
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-GOV-DOCUMENTS
  - VEP-GOV-CHANGE
  - VEP-DOC-REGISTRY
  - VEP-GOV-AI-INSTRUCTIONS
---
# 文書メタデータSchema

## 1. Scope

`docs/registry/documents.yaml`は、登録文書ごとに`metadata_mode`を指定します。すべての
Markdownへ同一形式のfront matterを強制しません。

許容値:

- `front-matter`
- `registry-only`
- `non-markdown`

Skillの`SKILL.md`と`.github/instructions/*.instructions.md`は各機能の専用metadata契約を
使用し、文書台帳の対象外です。

## 2. Registry entry

各entryの必須field:

- `document_id`
- `path`
- `document_type`
- `status`
- `canonical`
- `canonical_for`
- `owners`
- `metadata_mode`

文書台帳は、これらのfieldに対する唯一のSoTです。

## 3. `front-matter` mode

必須field:

- `schema_version`
- `document_id`
- `title`
- `document_type`
- `status`
- `canonical`
- `canonical_for`
- `owners`
- `supersedes`
- `superseded_by`
- `related_documents`

`document_id`、`document_type`、`status`、`canonical`、`canonical_for`、`owners`は台帳と
一致しなければなりません。

## 4. Provenance fields

次のfieldは過去文書との互換性のため存在しても構いませんが、現在の必須fieldでもSoTでも
ありません。

- `created_at`
- `updated_at`
- `authors`
- `reviewers`
- `source_issue`
- `change_control`

authorship、編集日時、diffはGit commit／PR、reviewは対象commitに結び付いたPR reviewまたは
明示的なreview recordを正本とします。意味変更のたびに`updated_at`を手動同期しません。

## 5. `registry-only` mode

- 汎用YAML front matterを持たない
- 先頭から`#`見出し等の利用可能な本文を開始する
- 文書metadataは台帳だけに保持する
- 本文内に正本への参照や短い運用規則を置くことはできる

対象例:

- `README.md`
- `AGENTS.md`
- `CLAUDE.md`
- `GEMINI.md`
- `.github/copilot-instructions.md`
- `CONTRIBUTING.md`
- `SECURITY.md`

## 6. Historical conditions

historical文書は`front-matter` modeとし、次を満たします。

- `canonical: false`
- `status: historical`
- `canonical_for: null`
- `historical_reason`
- `historical_at`
- `superseded_by` field

置換先がある場合は文書ID、責務廃止の場合はnullです。

## 7. Vendor-specific front matter

- `.github/instructions/*.instructions.md`: `applyTo`を保持する
- `SKILL.md`: portableな`name`と`description`を保持する

これらを汎用文書metadataへ拡張しません。lifecycle、owner、side effect、evalは
`.agents/skill-system.toml`をSoTとします。

## 8. Integrity rules

- registry entryの`metadata_mode`は必須かつ許容値である
- `front-matter`はMarkdownだけに使用する
- `registry-only`はMarkdownだけに使用し、汎用front matterを禁止する
- `non-markdown`をMarkdownへ使用しない
- front matterとregistryのprojection fieldが一致する
- `supersedes`、`superseded_by`、`related_documents`は登録済みIDを参照する
- 同一`canonical_for`に複数canonical文書を認めない

`scripts/validate_repository.py`とCIがfail closedで検証します。
