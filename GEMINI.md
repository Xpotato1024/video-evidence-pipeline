---
schema_version: "1.0"
document_id: VEP-ADAPTER-GEMINI
title: "Gemini CLI adapter"
document_type: agent-adapter
status: active
canonical: true
canonical_for: gemini-cli-entrypoint
created_at: 2026-08-06T16:28:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-POLICY", "VEP-AGENT-GOV", "VEP-AGENTS"]
---
# Gemini CLI instructions

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

作業開始前に`.agents/policy.md`、`.agents/governance.md`、対象Issue／PR、
`docs/registry/documents.yaml`を読んでください。本ファイルは入口であり、共通ポリシーを
複製・上書きしません。

認証情報・実メディア・ユーザー成果物をコミットせず、draft Skillを実運用しません。
変更後はlint、tests、repository validationを実行してください。
