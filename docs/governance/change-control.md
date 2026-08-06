---
schema_version: "1.0"
document_id: VEP-GOV-CHANGE
title: "文書変更統制"
document_type: governance
status: active
canonical: true
canonical_for: document-change-control
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
related_documents: ["VEP-GOV-DOCUMENTS", "VEP-GOV-METADATA", "VEP-DOC-REGISTRY", "VEP-ADR-INDEX"]
---
# 文書変更統制

## 1. 基本フロー

```text
Issue
  ↓
影響するcanonical責務を特定
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

## 2. 新規canonical文書

1. 既存の`canonical_for`と重複しないか確認する。
2. 文書IDを発行する。
3. canonicalテンプレートを使用する。
4. `documents.yaml`へ登録する。
5. `docs/README.md`または適切な索引からリンクする。
6. `status: draft`で開始し、レビュー後に`active`へ変更する。

## 3. 通常更新

意味的な更新では次を同一PRで行います。

- 本文を更新
- `updated_at`を更新
- 新たな主要著者がいれば`authors`へ追加
- 関連文書への影響を確認
- 必要ならADRを追加
- 台帳のpath/status/canonical_forが変わる場合は更新

軽微な誤字、format、リンク修正ではhistorical snapshotを作りません。

## 4. Historical snapshotが必要な変更

次の変更は、旧設計を将来参照する価値が高いため、原則としてhistorical snapshotを作ります。

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

例:

```text
docs/historical/2026-10-01/architecture/pipeline-architecture.md
```

## 5. Canonical文書の置換

1. 旧文書をhistorical pathへコピーする。
2. 旧文書を `canonical: false`、`status: historical`、`canonical_for: null` に変更する。
3. `historical_at`、`historical_reason`、`superseded_by`を設定する。
4. 新文書に `supersedes` を設定する。
5. `documents.yaml`のcanonical pathを新文書へ切り替える。
6. 索引と参照リンクを更新する。
7. 一つのcommitまたは不可分なPR内で行い、canonical不在・重複期間を作らない。

## 6. 廃止

責務自体が不要になり置換先がない場合:

- canonical文書をhistorical化する。
- `superseded_by: null` を許容する代わりに `historical_reason` へ廃止理由を書く。
- 台帳のcanonical責務を削除し、historical entryのみ残す。
- 関連する実装、Schema、Skillの廃止Issueを明示する。

## 7. ADRが必要な変更

以下の条件を一つでも満たす場合、ADRを作成します。

- 複数の妥当な技術選択肢から一つを選ぶ。
- 後から変更するコストが大きい。
- セキュリティ、プライバシー、監査独立性へ影響する。
- 既存のaccepted ADRを覆す。
- 設計文書だけでは「なぜ」を保存できない。

ADR作成後、architecture文書へ現在の結論を反映します。

## 8. Review

文書のreviewは次を確認します。

- 正本責務の重複
- 本文とfront matterの整合
- 台帳との整合
- historical化の要否
- ADRの要否
- 実装・Schema・Skillへの波及
- 認証情報、個人情報、配布限定情報の混入
- 「実装済み」「確認済み」等の過剰な状態表現

## 9. 緊急変更

秘密情報流出等の緊急時は、最小限の削除・無効化を先行できます。ただし、事後にIssueとPRを作成し、変更理由、影響、再発防止を正本へ反映します。Git履歴から秘密情報が消えたとは限らないため、credential rotationを優先します。
