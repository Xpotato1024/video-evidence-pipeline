---
schema_version: "1.0"
document_id: VEP-AGENTS
title: "Repository Agent Instructions"
document_type: agent-policy
status: active
canonical: true
canonical_for: repository-agent-policy
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
related_documents: ["VEP-DOCS-INDEX", "VEP-GOV-DOCUMENTS", "VEP-GOV-CHANGE", "VEP-ARCH-AGENTS"]
---
# AGENTS.md

このファイルは、リポジトリ内で作業するAIエージェントに適用する不変条件を定義します。詳細設計の正本ではなく、正本への参照順序と作業上の禁止事項を定めるものです。

## 1. 参照順序

作業開始時は次の順序で確認してください。

1. 対象IssueとPull Request
2. `docs/registry/documents.yaml`
3. `docs/README.md`
4. 対象責務のcanonical文書
5. 関連するaccepted ADR
6. 実装済みSchema、テスト、コード
7. historical文書は背景確認に限って参照する

canonical文書と実装が矛盾する場合、推測でどちらかへ合わせてはいけません。矛盾を所見として明示し、対象Issueのスコープ内で解消してください。

## 2. SoTと文書管理

- Markdown文書には `docs/governance/metadata-schema.md` に従うYAML front matterを付ける。
- 同一の `canonical_for` に対して `canonical: true` の文書を複数作らない。
- 文書を意味的に変更した場合は `updated_at` を更新する。
- canonical文書を置換する場合は、必要に応じて旧版を `docs/historical/` に保存し、`supersedes`／`superseded_by` を相互に設定する。
- 文書の追加、移動、canonical状態変更時は `docs/registry/documents.yaml` を同一変更で更新する。
- READMEやAGENTS.mdへ詳細設計を重複記載しない。正本へのリンクを置く。
- Git履歴だけを重要な設計状態の保存手段にしない。

## 3. データと認証情報

- 利用権限が確認されていないURLや動画を取得しない。
- ダウンロード可能であることを利用許諾と解釈しない。
- Cookie、Authorization header、API token、署名付きURLをコミット、ログ、Issue、PR本文へ残さない。
- 実動画、音声、字幕、抽出フレーム、文字起こし、ユーザー成果物をGitへ追加しない。
- 外部AI APIへの送信は、ジョブ設定で明示的に許可された場合だけ行う。
- raw sourceは不変とし、正規化済み・AI生成済みデータで上書きしない。

## 4. 実装原則

- 決定論的処理、意味処理、人間判断を分離する。
- すべての派生成果物に入力hash、生成手段、生成日時、tool/model versionを追跡できるlineageを持たせる。
- AI出力を自由文だけで次工程へ渡さず、Schema検証可能なJSON／JSONLを併設する。
- URL取得元、文字起こしbackend、export形式はadapterとして分離する。
- 失敗を黙って補完しない。不明、低確度、未実行を明示する。
- 実行していないテスト、取得、監査を成功したと報告しない。

## 5. AI役割分離

- authoringとindependent auditを同一会話コンテキストで完結させない。
- 監査者は作者のclaim ledgerを正解として扱わず、本文から主張を再抽出する。
- reviserは監査所見と証拠に基づいて修正し、根拠不足を新しい断定で埋めない。
- `P0 = 0` かつ `P1 = 0` になるまで人間承認待ちへ進めない。
- AIはhuman approvalを代行・推定・自動生成しない。

## 6. GitHub運用

- 原則として `codex/<issue-number>-<scope>` ブランチを使用する。
- 作業はDraft PRとして提出し、Issueはマージまでopenを維持する。
- ユーザーから明示されない限り、PRのReady化、merge、Issue close、branch削除を行わない。
- 一つのPRには一つの主要目的を持たせる。
- スコープ外の問題は無断で併合せず、所見または後続Issue候補として残す。
- commit前に変更ファイル、秘密情報、メディア混入、文書台帳整合性を確認する。

## 7. 言語と記述

- リポジトリ文書は原則として日本語を用いる。
- コード、識別子、Schema field、CLI名は英語を用いる。
- 規範文書では曖昧な努力目標ではなく、MUST／SHOULD／MAY相当の強度を明確にする。
- 日時はISO 8601形式とし、原則として明示的なタイムゾーンを含める。
