---
schema_version: "1.0"
document_id: VEP-README
title: "Video Evidence Pipeline"
document_type: repository-overview
status: draft
canonical: true
canonical_for: repository-overview
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
related_documents: ["VEP-DOCS-INDEX", "VEP-AGENTS", "VEP-ARCH-PIPELINE", "VEP-GOV-DOCUMENTS"]
---
# Video Evidence Pipeline

認可済みの動画を取得・正規化し、検証可能な証拠層を構築したうえで、レポート・要約等の生成、独立監査、修正、人間承認、最終出力までを扱うローカル中心のパイプラインです。

> [!IMPORTANT]
> 現在は設計基盤の構築段階です。取得・変換・文字起こし・AI実行の実装はまだ存在しません。

## 目的

このリポジトリは、AIが動画を直接「理解したつもり」で文章を生成する方式ではなく、次の順序を強制することを目的とします。

1. 利用権限とユーザー要件を確認する。
2. URLまたはローカルファイルから原本を取得する。
3. 動画・音声・字幕・フレームを再現可能な形へ正規化する。
4. 発話、画面、時刻、出所を結び付けた証拠パッケージを構築する。
5. 証拠に基づいて成果物を生成する。
6. 別コンテキストで主張と根拠を独立監査する。
7. 指摘に基づく修正ループを行う。
8. 人間が内容と提出適否を確認する。
9. 承認済み版をMarkdown、DOCX、PDF等へ出力する。

## 設計SoT

設計文書の入口は [`docs/README.md`](docs/README.md) です。

| 責務 | 正本 |
|---|---|
| 文書ガバナンス | [`docs/governance/document-governance.md`](docs/governance/document-governance.md) |
| 文書メタデータ | [`docs/governance/metadata-schema.md`](docs/governance/metadata-schema.md) |
| 変更統制 | [`docs/governance/change-control.md`](docs/governance/change-control.md) |
| システム境界 | [`docs/architecture/system-context.md`](docs/architecture/system-context.md) |
| パイプライン構造 | [`docs/architecture/pipeline-architecture.md`](docs/architecture/pipeline-architecture.md) |
| 成果物モデル | [`docs/architecture/artifact-model.md`](docs/architecture/artifact-model.md) |
| 状態遷移 | [`docs/architecture/state-machine.md`](docs/architecture/state-machine.md) |
| 信頼境界 | [`docs/architecture/trust-boundaries.md`](docs/architecture/trust-boundaries.md) |
| 技術基準 | [`docs/architecture/technology-baseline.md`](docs/architecture/technology-baseline.md) |
| AI／人間の役割 | [`docs/architecture/agent-operating-model.md`](docs/architecture/agent-operating-model.md) |
| 文書台帳 | [`docs/registry/documents.yaml`](docs/registry/documents.yaml) |

詳細な設計内容をREADMEへ複製しません。競合が生じた場合は、文書台帳で指定されたcanonical文書を確認し、矛盾をPRで解消します。

## データ取扱い

実際の講義動画、音声、字幕、Cookie、アクセストークン、署名付きCDN URL、課題成果物はGit管理しません。Privateリポジトリであっても、認証情報や配布限定コンテンツを保存してよい根拠にはなりません。

実データ処理は将来のローカルワークスペース `.local/` 配下で行い、GitHub Actionsでは合成fixtureのみを使用します。

## 開発状態

- Tracking Issue: [#1 設計SoTと文書ガバナンス基盤を構築する](https://github.com/Xpotato1024/video-evidence-pipeline/issues/1)
- 実装言語、依存関係、CLI、Schema、Skill、CIは後続Issueで導入します。
- `main`への変更は原則としてIssueに紐づくPull Request経由で行います。
