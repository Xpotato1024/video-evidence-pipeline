---
schema_version: "1.0"
document_id: VEP-AGENT-GOV
title: "Skillガバナンス"
document_type: governance
status: active
canonical: true
canonical_for: agent-skill-governance
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-POLICY", "VEP-ARCH-AGENTS", "VEP-OPS-CI"]
---
# Skillガバナンス

## 1. 原則

Skillは再利用可能な意味処理手順です。FFmpeg、hash、Schema validation等の決定論的処理を
Skill本文へ再実装しません。Skillの正本一覧は`skill-system.toml`です。

## 2. Lifecycle

- `observed`: 再発する作業として観測しただけ
- `candidate`: Skill化候補。使用不可
- `draft`: 契約・eval作成中。実運用不可
- `active`: 明示選択して利用可能
- `deprecated`: 既存利用の移行期間。新規利用不可
- `rejected`: Skill化しない判断を記録

`draft`から`active`への昇格には、必要artifact／scriptが実在し、eval合計9点以上、
重大な未解決所見なし、side-effect boundary明確、owner reviewを要求します。

## 3. Activation

既定は`explicit-only`です。`automatic_trigger = true`は将来の候補選択を許可するだけで、
外部送信、取得、書込み、GitHub mutationを自動承認しません。

## 4. Side effect policy

- `read-only`: 読取りと検査のみ
- `workspace-write`: ローカル派生成果物だけを書込む
- `repository-write`: リポジトリ変更を作成する
- `external-write`: 外部サービスへ変更する
- `instruction-only`: 契約定義のみで実行不可

`external-write`はSkill単独で許可せず、実行時の明示承認を必要とします。

## 5. Skill contract

各`SKILL.md`は、目的、適用条件、非適用条件、入力、出力、手順、停止条件、検証、禁止事項を
持ちます。YAML front matterはportableな`name`と`description`に限定し、lifecycle等は
`skill-system.toml`を正本とします。

## 6. Eval

各active/draft Skillは`.agents/skill-evals/<skill-id>.toml`を持ちます。evalは最低2ケースを
含み、正常系だけでなく、権限不足、証拠不足、独立性不足等の拒否・停止条件を評価します。

各caseは5個のmachine-readableなbinary criterionを持ち、1 criterionを1点として採点します。
criterionは観測された結果またはSkill契約が条件を満たす場合だけpassとし、印象による部分点を
付けません。caseは全criterionがpassし、`forbidden`の行動が1件も観測されない場合だけpassです。
2 caseの標準evalは10点満点で、合計点はcase scoreの和として再計算します。

active化には点数だけでなく、全case pass、P0=0、P1=0、必要artifactの実在、side-effect boundary、
Skill本文とeval定義のSHA-256へ束縛した評価結果、評価対象commit、evaluator identity／method／日時、
finding summary、owner reviewを要求します。評価結果はeval定義と自己参照させず、
`.agents/skill-eval-results/<skill-id>.toml`へ分離します。active Skillの評価結果がcurrent Skill本文または
eval定義と一致しない場合はstaleとしてrepository validationを失敗させます。

## 7. Provider adapter

AI固有の入口は共通ポリシーを上書きしません。vendor固有機能を利用する場合も、Skill ID、
入力bundle、出力contract、side effectを共通形式で記録します。
