---
schema_version: "1.0"
document_id: VEP-ADAPTER-CLAUDE
title: "Claude Code adapter"
document_type: agent-adapter
status: active
canonical: true
canonical_for: claude-code-entrypoint
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
# Claude Code instructions

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

@.agents/policy.md
@.agents/governance.md

対象Issue／PRと文書台帳を先に読み、共通ポリシーを優先してください。認証情報・実メディア・
ユーザー成果物をGitへ追加せず、実行していない検証を成功と報告しないでください。

変更後は`uv run ruff check scripts tests`、`uv run pytest -q`、
`uv run python scripts/validate_repository.py --root .`を実行します。
