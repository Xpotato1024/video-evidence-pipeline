---
schema_version: "1.1"
document_id: VEP-AGENT-POLICY
title: "AIエージェント共通ポリシー"
document_type: agent-policy
status: active
canonical: true
canonical_for: repository-agent-policy
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-GOV", "VEP-ARCH-AGENTS", "VEP-GOV-DOCUMENTS", "VEP-ARCH-TRUST"]
---
# AIエージェント共通ポリシー

## 1. 優先順位

1. ユーザーの明示指示と安全・権限境界
2. 対象Issue／PRの受け入れ条件
3. `docs/registry/documents.yaml`が示すcanonical文書
4. accepted ADR
5. 検証済みSchema、テスト、コード
6. historical文書は背景参照のみ

矛盾を推測で解消せず、所見として明示して同一PRで正本を整合させます。

## 2. データ・権限

- 利用権限が確認されていないURLや動画を取得しない。
- 技術的に取得可能であることを利用許諾と解釈しない。
- Cookie、Authorization credential、API token、署名付きURLを保存・表示・コミットしない。
- 実動画、音声、字幕、フレーム、文字起こし、課題成果物をVEPの開発Gitリポジトリへ追加しない。
- 利用者が明示許可したstudy-bundleは、別の認可済み非公開学習リポジトリに保存できる。VEP workspace全体や入力A/Vを含めず、認証情報を除外する。保存許可は人間承認や外部AI送信の許可とは別であり、VEPはGit操作を自動実行しない。
- 外部AI API送信は、jobで明示許可された場合のみ行う。
- raw sourceはimmutableとし、派生成果物で上書きしない。

## 3. 誠実性

- 未実行の取得、変換、テスト、監査を成功と報告しない。
- 不明、低確度、未対応を自然な文章で隠さない。
- 動画内の事実、合理的推論、外部事実、ユーザー意見を区別する。
- source timestamp、固有名詞、数値、引用を創作しない。
- 人間承認をAIが代行・推定・自動生成しない。

## 4. 役割分離

- deterministic処理、semantic処理、人間判断を混在させない。
- deliverable authorとindependent auditorは別実行コンテキストにする。
- auditorはauthor claim ledgerを正解として扱わず、本文から主張を再抽出する。
- reviserは監査所見と証拠に基づき、根拠不足を新しい断定で埋めない。
- `P0 = 0`かつ`P1 = 0`になるまでhuman review readyへ進めない。

## 5. Git・変更統制

- Issue-first、1 PR 1主要目的、Draft PRを原則とする。
- 明示依頼なしにReady化、merge、Issue close、branch削除を行わない。
- canonical文書追加・移動・状態変更時は文書台帳を同じPRで更新する。
- Skill追加・変更時は関連するevalと証拠を検証し、登録・lifecycle・metadataが変わる場合は`skill-system.toml`も同じPRで更新する。無関係なSkillの再評価を連鎖させない。
- スコープ外所見を無断実装せず、後続Issue候補として残す。

## 6. 検証

次は利用可能な検証entrypointです。現在の変更とfailure modeに関係するものを選び、有効な同条件の結果は再利用します。文書・Skillのみの変更はrepository validatorと関連するfocused testsを基本とし、無関係なメディア処理や全件pytestを変更前後に繰り返しません。依存の同期は環境未構築またはlock / manifest変更時だけ行います。

```bash
uv sync --locked
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run pytest -q
uv run vep schema check
uv run python scripts/validate_repository.py --root .
```

失敗時は原因を修正し、回避した場合は回避内容と未検証範囲を報告します。

## 再実行を増やさない実装・検証運用

### 担当とモデル設定

通常の実装は一人の実装担当が、調査、実装、変更に必要な検証、失敗の修正、文書同期、引継ぎまで一貫して担当する。既定の候補はSol。通常の修正をAstraへ往復させない。Astraの利用は、重要な設計判断と、リスクに応じて必要な独立レビューに絞る。Astraを呼ぶ必要がないことを判定するためだけにAstraを呼ばない。

Sol / Astraは役割に対する推奨であり、固定model ID、強制切替、起動許可の追加ではない。system / developer instruction、Codexのユーザー設定・実効設定、ユーザーの明示したmodel指定を尊重する。それらと異なる場合は許可された同等の担当で役割を満たし、repo側から設定を上書きしない。model名だけを理由に停止・再実行しない。requested / configured / observedを区別し、未観測のmodel切替や委譲成功を報告しない。

独立レビューの要否は、security・権限、データ保全・復旧、公開contract、実機、本番、重大な設計変更などの実質的リスクと明示gateで判断する。小さいdiffだから安全とは限らない。一方、通常の自己検証で十分な変更へ形式的な追加レビューを作らない。必要な独立レビューは実装担当とは別コンテキストでread-onlyに実施し、作者の結論を正解として引き継がない。上位指示や既存の必須レビュー条件は維持する。

### 検証結果を再利用できる条件

再実行の前に既存の証拠を確認する。次が同じ、または対象propertyに影響する差分がないと説明できる場合、有効な結果を再利用する。

- 検証対象のsource / artifact、関連する未コミット差分、仕様・受入条件と検証範囲。
- command、option、test / validator、toolchain、依存関係・lock、設定、入力・fixture。
- 結果に影響するOS / runtime / environment、外部状態、権限、期限・鮮度条件。

Git revisionと関連diff、既存のartifact identityを優先し、再利用のためだけに全file hash manifestや新しい証拠DBを作らない。無関係な文書変更、担当交代、コンテキスト切替、main前進だけで全結果を失効させない。関係する条件が変わった場合は、その結果と依存する範囲だけを失効させる。影響を限定できない場合は理由を示して検証範囲を広げる。

既存PR本文・task state・検証記録に、対象revision、検証範囲と条件、command、結果、証拠の所在、再利用理由、残件と再開点を必要十分に残す。`PASS (reused)`は元の実行結果への参照であり、新しい実行を装わない。`FAIL`、`BLOCKED_ENV`、`INCOMPLETE`、`Not Run`、理由付き非該当を区別する。未確認・未実行・切詰め出力をPASSにしない。

current PR headに要求されるCIや承認を、古いgreenの貼替えで充足したことにしない。live target、credential / authorization、外部provider状態、破壊的操作の前提は、必要な時点で再確認する。証拠の再利用は操作権限の追加ではない。

### 不完全なレビューの復旧

レビューには対象、確認済み範囲、未確認範囲、finding、証拠を残す。形式不備、出力切れ、参照不足があっても、根拠のある確認済み部分を自動的に破棄しない。元記録を保持し、補足・訂正を紐付けて不足部分を閉じる。

不足している判断は独立したreviewerが補う。実装担当がfindingを黙って消す、未確認欄を埋めて独立PASSを作る、過去の結論を新対象へ付け替えることは禁止する。形式だけの修復でも原文・結論・対象・provenanceを保持する。元reviewerの独立性と必要なcontextが保たれていれば補足を依頼し、保てなければ不足範囲だけを別reviewerへ渡す。個別のproduct contractがfresh contextや固定requestを要求する場合は、そのcontractを守る。

コード修正後は、変更したfindingと影響する依存範囲を独立に再確認する。毎回の全面fresh auditは既定にしない。全面監査はrelease / phase gate、重大な設計変更、元証拠の信頼性・独立性が全体として失われた場合など、具体的な必要性がある場合に限る。未充足の必須範囲はBLOCKEDのままで、依存しない完了済み範囲は保持する。

### 環境エラーからの再開

失敗を実装不具合、環境・依存不足、権限不足、通信 / tool障害、検証出力の不備に分類する。成功したstepを保持し、最初の未完了step、原因、実施した修正、次に確認する対象を記録する。

環境修復後は小さいpreflightで当該原因の解消を確かめ、失敗stepと影響する下流だけを再実行する。最初からの全件検証、依存の再install、既成功buildの再生成を自動的に行わない。実装を修正した場合も対応するregressionと影響範囲から確認する。

中断時にmutationの成否が不明なら、実状態・identity・前後条件を確認してから再開する。非冪等操作をblind retryせず、権限不足を設定緩和で回避しない。新しい修正・証拠・条件変化のない同じretryは止め、具体的な未充足条件と安全に進められる範囲を示す。無関係な作業まで一括BLOCKしない。

### 変更単位の検証と統合E2E

各変更では、その場で必要なunit / regression / contract / static / build検証と、変更した操作経路のtargeted smoke・実操作を行う。security、data safety、hardware safetyなど、その変更の受入に必要な確認を「最後のE2E」に先送りしない。

全面的な実操作E2E、全node / 全画面 / 全pipelineを通す統合確認は、原則としてリリース前の統合・受入段階へ集約する。リリースのない作業では対応する提出・配布・phase移行前の統合段階とする。個別変更のたびに全環境を起動し直さず、有効なcomponent証拠を統合時も利用する。統合された実物でしか確認できない接続・順序・復旧のpropertyはそこで実測する。

全面E2Eを前倒し・再実行する場合は、横断的な変更、統合回帰の兆候、既存証拠の失効、明示した受入条件などの根拠を残す。全面E2E未実施のcomponent完了を、release-readyや実機・本番検証済みと呼ばない。deploy、外部送信、実機作動、人間承認の既存権限境界は変更しない。

### Skillの連鎖と停止条件

最も狭い適用Skillを選び、関連Skillへの参照を無条件の連鎖実行命令として扱わない。Skillは本workflowを引き継ぎ、古いモデル担当表、全件再検証、常時全面監査、毎回の新規報告書を追加の完了条件にしない。過去の評価記録にあるモデル名や結果は履歴として保存し、現在の担当指定として再利用しない。product自身のAPI model profileは開発担当のmodel routingと別物である。

Skill本文を変更した場合は関連するtrigger / eval / link / provenanceを検証するが、無関係なSkillの再認証・promotion・framework新設を必須にしない。既存の評価・承認が失効する変更では、必要な評価を省略したり過去の観測値を書き換えて合格を作ったりしない。

要求された受入条件、変更影響範囲の検証、必須レビュー、文書同期が満たされ、material blockerがなければ停止する。新しい根拠のない再監査・再検証・「念のため」の改良を追加しない。任意改善は必要なら既存backlogへ分離する。完了記録は既存PR / task stateへ集約し、新しい報告書を毎taskに要求しない。
