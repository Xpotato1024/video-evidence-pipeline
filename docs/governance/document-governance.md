---
schema_version: "1.0"
document_id: VEP-GOV-DOCUMENTS
title: "文書ガバナンス"
document_type: governance
status: active
canonical: true
canonical_for: document-governance
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
related_documents: ["VEP-GOV-METADATA", "VEP-GOV-CHANGE", "VEP-DOC-REGISTRY", "VEP-DOCS-INDEX"]
---
# 文書ガバナンス

## 1. 目的

設計、実装、AIエージェントの指示、過去判断を混同せず、現在有効な情報へ一意に到達できる状態を維持します。

## 2. 文書分類

### 2.1 Canonical document

特定の責務について、現在有効な唯一の正本です。

要件:

- `canonical: true`
- `canonical_for` が非null
- `status` は原則 `draft` または `active`
- 同一の `canonical_for` を持つcanonical文書は一つだけ
- `docs/registry/documents.yaml` に登録される
- 変更はPull Requestで行う

`draft` のcanonical文書は、その責務に対する現在の設計案の正本ですが、確定・実装済みを意味しません。

### 2.2 Historical document

過去に有効だった設計、重要な移行前状態、廃止された仕様を保存したものです。

要件:

- `canonical: false`
- `status: historical`
- `canonical_for: null`
- `historical_at` と `historical_reason` を持つ
- `superseded_by` fieldを明示し、置換先がある場合は文書ID、責務廃止の場合はnullとする
- ファイル先頭にhistorical警告を表示する
- 現在の設計判断へ直接使用しない

### 2.3 Decision record

ADRは、重要な判断の背景、選択肢、理由、影響を保存します。accepted ADRは過去の判断根拠として有効ですが、現行仕様の完全な正本ではありません。後続ADRによりsupersededとなり得ます。

### 2.4 Operational entrypoint

README、AGENTS.md、索引は利用者やAIを正本へ案内します。詳細仕様を複製してはいけません。

### 2.5 Generated reference

Schemaやツールから生成される文書です。生成元を正本とし、生成物を直接編集しません。

## 3. SoTの優先関係

同一責務内では、次の優先関係を用います。

1. 実行時に機械検証されるSchema、contract test、security gate
2. `documents.yaml` が指すcanonical文書
3. accepted ADRの判断理由
4. README、AGENTS.md、運用ガイド
5. historical文書

ただし、上位と下位の矛盾を放置してよいという意味ではありません。矛盾を検出した時点で、影響範囲を特定し、正本と実装を同一または連続するPRで同期します。

設計段階で実装が存在しない場合、canonical文書が現在のSoTです。

## 4. 台帳とfront matter

- `docs/registry/documents.yaml` は文書ID、path、canonical責務、状態の正本台帳です。
- 各Markdownのfront matterは、その文書自身の作成者、日時、関係文書、置換関係を保持します。
- 台帳とfront matterが矛盾した場合はfail closedとし、どちらかを暗黙に優先しません。
- 文書移動時は文書IDを維持し、pathのみ変更します。
- 文書の責務が根本的に変わる場合は新しい文書IDを発行します。

## 5. 文書ID

形式:

```text
VEP-<CATEGORY>-<NAME>
```

推奨category:

- `ARCH`: architecture
- `GOV`: governance
- `ADR`: architecture decision record
- `OPS`: operations
- `SEC`: security
- `SKILL`: AI Skill
- `TEMPLATE`: template

IDはpathに依存せず、不変です。削除済みIDを別文書へ再利用してはいけません。

## 6. Canonical化の条件

文書を`active`へ変更する前に、最低限次を確認します。

- 責務と非責務が明確
- 関連文書との重複がない
- 用語が一貫
- 未確定事項が明示
- 実装がある場合は実装と整合
- `documents.yaml` とfront matterが一致
- historicalへの影響が処理済み

## 7. 文書の粒度

一つのcanonical文書は一つの主要責務を持ちます。複数文書に同じ表、ルール、状態一覧を複製せず、正本を一つにして参照します。

例:

- stateの名称と遷移条件はstate-machine文書が正本
- artifact classとlineageはartifact-model文書が正本
- 技術の採用状態はtechnology-baseline文書が正本
- AGENTS.mdはこれらを再定義せず、参照と作業制約だけを保持する

## 8. 禁止事項

- 日付や作成者をGit履歴からのみ推定する運用
- historical文書をcanonical文書と同じ場所に無印で残すこと
- READMEに完全な設計仕様を埋め込むこと
- 一つの変更でcanonicalを二つ存在させること
- old pathの文書を内容だけ書き換えて履歴を消すこと
- AIがレビュー済み・人間承認済みを自己申告すること
