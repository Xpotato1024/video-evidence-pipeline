---
schema_version: "1.1"
document_id: VEP-GOV-DOCUMENTS
title: "文書ガバナンス"
document_type: governance
status: active
canonical: true
canonical_for: document-governance
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-GOV-METADATA
  - VEP-GOV-CHANGE
  - VEP-DOC-REGISTRY
  - VEP-DOCS-INDEX
---
# 文書ガバナンス

## 1. 目的

設計、実装、AIエージェントの指示、過去判断を混同せず、現在有効な情報へ一意に到達できる
状態を維持します。

## 2. 文書分類

### 2.1 Canonical document

特定の責務について、現在有効な唯一の正本です。

要件:

- `canonical: true`
- `canonical_for`が非null
- `status`は原則`draft`または`active`
- 同一の`canonical_for`を持つcanonical文書は一つだけ
- `docs/registry/documents.yaml`へ登録する
- 変更はPull Requestで行う

`draft`のcanonical文書は現在の設計案の正本ですが、確定・実装済みを意味しません。

### 2.2 Historical document

過去に有効だった設計、重要な移行前状態、廃止された仕様を保存したものです。

要件:

- `canonical: false`
- `status: historical`
- `canonical_for: null`
- `historical_at`と`historical_reason`を持つ
- `superseded_by` fieldを明示し、置換時は文書ID、責務廃止時はnullとする
- ファイル先頭の本文にhistorical警告を表示する
- 現在の設計判断へ直接使用しない

### 2.3 Decision record

ADRは重要な判断の背景、選択肢、理由、影響を保存します。accepted ADRは判断根拠として
有効ですが、現行仕様全体の正本ではありません。

### 2.4 Operational entrypoint

README、AGENTS.md、AI provider adapter、CONTRIBUTING、SECURITYは、人間またはツールを
正本へ案内する入口です。即時可読性と自動検出互換性を優先し、詳細仕様を複製しません。

### 2.5 Generated reference

Schemaやツールから生成される文書です。生成元を正本とし、生成物を直接編集しません。

## 3. Metadata mode

文書台帳の`metadata_mode`が、各登録文書のmetadata表現を決定します。

### `front-matter`

architecture、governance、operations、ADR、historical等の内部設計文書に使用します。

- 文書先頭に最小限の意味的YAML front matterを持つ
- front matterは文書ID、状態、正本責務、owner、置換関係の局所projectionである
- 台帳との一致をvalidatorで強制する

### `registry-only`

README、AI自動検出入口、community health文書に使用します。

- 汎用YAML front matterを持たない
- 先頭から人間またはAIが利用する本文を開始する
- 文書ID、分類、状態、正本責務、ownerは台帳だけで管理する

### `non-markdown`

`documents.yaml`等のMarkdownではない登録対象に使用します。

### Vendor-specific metadata

`.github/instructions/*.instructions.md`の`applyTo`や`SKILL.md`の`name`／`description`は、
対象ツールの実行契約です。文書ガバナンス用front matterとは別物として扱い、
`documents.yaml`の対象にしません。

## 4. SoTの分担

| 情報 | SoT |
|---|---|
| 文書ID、path、分類、状態、正本責務、owner、metadata mode | `documents.yaml` |
| front-matter文書の局所的な意味metadata | 各文書のfront matter |
| authorship、編集日時、変更差分 | Git commit／Pull Request |
| review対象version、reviewer、結果 | GitHub PR reviewまたはcommit-bound review record |
| Skill lifecycle、side effect、eval | `.agents/skill-system.toml` |

front matterと台帳が競合した場合はfail closedとし、同じPRで整合させます。Git履歴から
意味的な正本責務を推測してはいけませんが、authorshipや更新時刻を文書へ二重転記もしません。

## 5. 文書ID

形式:

```text
VEP-<CATEGORY>-<NAME>
```

IDはpathに依存せず、不変です。削除済みIDを別文書へ再利用してはいけません。

## 6. Canonical化の条件

- 責務と非責務が明確
- 関連文書との重複がない
- 用語が一貫
- 未確定事項が明示
- 実装がある場合は実装と整合
- 台帳とmetadata modeに応じた文書表現が一致
- historicalへの影響が処理済み

## 7. 文書の粒度

一つのcanonical文書は一つの主要責務を持ちます。複数文書へ同じ表、ルール、状態一覧を
複製せず、正本を一つにして参照します。

## 8. 禁止事項

- registry-only文書へ汎用front matterを追加すること
- authorship、更新日時、reviewerをfront matterとGit／PRへ二重管理すること
- vendor-required front matterを文書metadataと誤認して削除すること
- historical文書をcanonical文書と同じ場所に無印で残すこと
- READMEへ完全な設計仕様を埋め込むこと
- 一つの変更で同一責務のcanonicalを二つ存在させること
- AIがレビュー済み・人間承認済みを自己申告すること
