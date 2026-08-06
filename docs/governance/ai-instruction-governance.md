---
schema_version: "1.0"
document_id: VEP-GOV-AI-INSTRUCTIONS
title: "AI指示・adapterガバナンス"
document_type: governance
status: active
canonical: true
canonical_for: ai-instruction-governance
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-POLICY", "VEP-AGENT-GOV", "VEP-ARCH-AGENTS", "VEP-OPS-CI"]
---
# AI指示・adapterガバナンス

## 1. 採用構造

`.agents/`をvendor-neutralな正本とし、AI製品が自動検出する入口は所定位置に残します。
単純に全ファイルを`.agents/`へ移動すると、自動読込みに依存する環境で指示が適用されない
ためです。

## 2. 責務

| 層 | 責務 |
|---|---|
| `.agents/policy.md` | 共通不変条件 |
| `.agents/governance.md` | Skill lifecycle、trigger、eval、side effect |
| `.agents/skill-system.toml` | Skillの機械可読SoT |
| `AGENTS.md`等 | vendor／tool固有の自動検出入口 |
| `.github/instructions/` | Copilotのpath-specific補足 |

adapterは共通規則を全文複製せず、marker、正本への参照、重大な安全規則、実行コマンドだけを
持ちます。

## 3. 優先順位

provider固有adapterは共通ポリシーを弱められません。provider機能との競合がある場合、
安全境界とSoTを維持し、機能を使用しないかIssueで設計変更します。

## 4. 変更手順

1. 共通規則の変更は`.agents/policy.md`または`.agents/governance.md`へ行う。
2. `skill-system.toml`、Skill、evalの整合性を更新する。
3. adapterのmarkerと短い直接規則を更新する。
4. 文書台帳を更新する。
5. repository validatorとCIを通す。

## 5. Portability

Skill front matterは`name`と`description`を基本とし、製品固有fieldへ依存しません。
実行履歴ではprovider／modelを記録しますが、Skill契約の意味はproviderに依存させません。
