---
schema_version: "1.1"
document_id: VEP-GOV-AI-INSTRUCTIONS
title: "AI指示・adapterガバナンス"
document_type: governance
status: active
canonical: true
canonical_for: ai-instruction-governance
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-AGENT-POLICY
  - VEP-AGENT-GOV
  - VEP-ARCH-AGENTS
  - VEP-OPS-CI
  - VEP-GOV-METADATA
---
# AI指示・adapterガバナンス

## 1. 採用構造

`.agents/`をvendor-neutralな正本とし、AI製品が自動検出する入口は所定位置に残します。

## 2. 責務

| 層 | 責務 | Metadata |
|---|---|---|
| `.agents/policy.md` | 共通不変条件 | 文書台帳の`front-matter` |
| `.agents/governance.md` | Skill lifecycle、trigger、eval、side effect | 文書台帳の`front-matter` |
| `.agents/skill-system.toml` | Skillの機械可読SoT | TOML contract |
| `AGENTS.md`等 | vendor／tool固有の自動検出入口 | 文書台帳の`registry-only` |
| `.github/instructions/*.instructions.md` | Copilot path-specific補足 | vendor-required `applyTo` |
| `SKILL.md` | portable Skill instructions | `name`／`description` |

adapterは共通規則を全文複製せず、marker、正本への参照、重大な安全規則、実行コマンドだけを
持ちます。汎用文書metadataを入口ファイルの先頭へ置きません。

## 3. 優先順位

provider固有adapterは共通ポリシーを弱められません。provider機能との競合がある場合、
安全境界とSoTを維持し、機能を使用しないかIssueで設計変更します。

## 4. 変更手順

1. 共通規則の変更は`.agents/policy.md`または`.agents/governance.md`へ行う。
2. `skill-system.toml`、Skill、evalの整合性を更新する。
3. adapterのmarkerと短い直接規則を更新する。
4. 文書台帳の`metadata_mode`を確認する。
5. path-specific instructionsの`applyTo`を保持する。
6. repository validatorとCIを通す。

## 5. Portability

Skill front matterは`name`と`description`を基本とし、製品固有fieldへ依存しません。
実行履歴ではprovider／modelを記録しますが、Skill契約の意味はproviderに依存させません。
