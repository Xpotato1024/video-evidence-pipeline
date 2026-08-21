---
schema_version: "1.0"
document_id: VEP-AGENT-INDEX
title: "AIエージェント運用索引"
document_type: agent-index
status: active
canonical: true
canonical_for: agent-system-index
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-20T18:30:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-POLICY", "VEP-AGENT-GOV", "VEP-ARCH-AGENTS"]
---
# AIエージェント運用索引

`.agents/`は、特定AI製品に依存しない共通ポリシー、Skill、eval、lifecycle管理の正本です。
一方、AIごとの自動検出入口はルートまたは`.github/`の規定位置に残します。

| 対象 | 正本 |
|---|---|
| 共通不変条件 | [`policy.md`](policy.md) |
| Skill lifecycle／trigger／eval | [`governance.md`](governance.md) |
| 機械可読Skill台帳 | [`skill-system.toml`](skill-system.toml) |
| 利用可能なSkill | [`skills/`](skills/) |
| Skill eval | [`skill-evals/`](skill-evals/) |
| Active Skill評価結果 | [`skill-eval-results/`](skill-eval-results/) |
| 未昇格候補 | [`skill-candidates/`](skill-candidates/) |
| 新規Skillテンプレート | [`templates/SKILL.md`](templates/SKILL.md) |

## Current Skill state

`repository-sot-audit`、`refine-video-transcript`、`analyze-video-evidence`、
`draft-video-report`、`draft-video-summary`、`independent-content-audit`、
`revise-from-audit`、`run-video-evidence-job`はactiveです。すべて明示選択専用
（`automatic_trigger = false`）で、Skill invocationは権限を追加しません。semantic responseを作る6 Skillは
`instruction-only`、public CLIでjobを進めるOperatorの`run-video-evidence-job`だけは`workspace-write`です。
各SkillにはSkill本文SHA、resource bundle fingerprint、eval定義SHA、評価対象commit、
case別fresh-context結果、P0/P1=0、owner reviewを含むcurrent result evidenceがあります。
`prepare-human-review`は再評価前のdraftです。

## Adapter

- `AGENTS.md`: agents.md／Codex互換入口
- `CLAUDE.md`: Claude Code入口
- `GEMINI.md`: Gemini CLI入口
- `.github/copilot-instructions.md`: GitHub Copilot入口

adapterは必要最低限の安全規則と正本への参照だけを持ちます。共通規則を変更する場合は、
先に`policy.md`または`governance.md`を更新し、adapterのmarkerと整合性を保ってください。
