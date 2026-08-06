---
schema_version: "1.1"
document_id: VEP-GOV-CHANGE
title: "文書変更統制"
document_type: governance
status: active
canonical: true
canonical_for: document-change-control
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-GOV-DOCUMENTS
  - VEP-GOV-METADATA
  - VEP-DOC-REGISTRY
  - VEP-ADR-INDEX
---
# 文書変更統制

## 1. 基本フロー

```text
Issue
  ↓
影響するcanonical責務とmetadata modeを特定
  ↓
ブランチ作成
  ↓
文書・台帳・関連リンクを同時変更
  ↓
機械検証
  ↓
Draft PR
  ↓
独立レビュー
  ↓
Ready／merge
  ↓
Issue close
```

## 2. 新規文書

1. 既存の`canonical_for`と重複しないか確認する。
2. 文書IDを発行する。
3. 用途に応じて`metadata_mode`を選ぶ。
4. `front-matter`ならcanonicalテンプレートを使用する。
5. `registry-only`なら本文から開始し、汎用front matterを追加しない。
6. `documents.yaml`へ登録する。
7. 適切な索引からリンクする。
8. 必要に応じて`status: draft`で開始する。

## 3. 通常更新

意味的な更新では次を同じPRで行います。

- 本文を更新する
- path、status、canonical責務、owner、metadata modeが変わる場合は台帳を更新する
- front-matter文書の意味metadataが変わる場合だけprojectionを更新する
- 関連文書への影響を確認する
- 必要ならADRを追加する

`updated_at`、`authors`、`reviewers`を文書へ手動追記・同期しません。Git commitとPR reviewが
対象versionのprovenanceを保持します。

軽微な誤字、format、リンク修正ではhistorical snapshotを作りません。

## 4. Metadata mode変更

`front-matter`と`registry-only`の切替は、文書の利用面とparser互換性に影響するため、
台帳、本文、validator testを同じPRで変更します。

- `registry-only`へ変更する場合は汎用front matterを除去する
- `front-matter`へ変更する場合は最小必須fieldを追加する
- vendor固有front matterを同じ変更として扱わない

## 5. Historical snapshotが必要な変更

- パイプライン層の追加・統合・削除
- artifact classまたはlineage規則の互換性破壊
- state名、遷移、gateの大幅変更
- 信頼境界、外部送信、秘密情報の扱いの変更
- author／auditor／human approvalの責務変更
- 既存実装の移行計画に影響する技術基準変更

保存先:

```text
docs/historical/<YYYY-MM-DD>/<元のdocs配下path>
```

## 6. Canonical文書の置換

1. 旧文書をhistorical pathへコピーする。
2. 旧文書を`canonical: false`、`status: historical`、`canonical_for: null`へ変更する。
3. `historical_at`、`historical_reason`、`superseded_by`を設定する。
4. 新文書に`supersedes`を設定する。
5. 台帳のcanonical pathを新文書へ切り替える。
6. 索引と参照リンクを更新する。
7. canonical不在・重複期間を作らない。

## 7. 廃止

責務自体が不要になり置換先がない場合:

- canonical文書をhistorical化する
- `superseded_by: null`とし、`historical_reason`へ廃止理由を書く
- 台帳のcanonical責務を削除し、historical entryを残す
- 関連実装、Schema、Skillの廃止Issueを明示する

## 8. ADRが必要な変更

- 複数の妥当な技術選択肢から一つを選ぶ
- 後から変更するコストが大きい
- セキュリティ、プライバシー、監査独立性へ影響する
- accepted ADRを覆す
- 設計文書だけでは「なぜ」を保存できない

## 9. Review

文書reviewは特定のcommitまたはPR diffに対して行います。

確認対象:

- 正本責務の重複
- metadata modeと本文表現
- 台帳との整合
- historical化／ADRの要否
- 実装・Schema・Skillへの波及
- 認証情報、個人情報、配布限定情報の混入
- 「実装済み」「確認済み」等の過剰な状態表現

reviewer名を文書へ永続的に残して最新版までreview済みに見せません。

## 10. 緊急変更

秘密情報流出等では最小限の削除・無効化を先行できます。事後にIssueとPRを作成し、変更理由、
影響、再発防止を正本へ反映します。credential rotationを優先します。
