---
schema_version: "1.0"
document_id: VEP-ADAPTER-COPILOT
title: "GitHub Copilot adapter"
document_type: agent-adapter
status: active
canonical: true
canonical_for: github-copilot-entrypoint
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
# GitHub Copilot instructions

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

Read `.agents/policy.md`, `.agents/governance.md`, the target Issue/PR, and the document registry before
changing files. Do not commit credentials, signed URLs, real media, transcripts, or user deliverables.
Do not treat draft Skills as executable. Keep authoring and independent audit in separate contexts.

Use issue-scoped Draft PRs. Run Ruff, pytest, and `scripts/validate_repository.py` before completion.
Do not claim a check passed unless its command was actually executed.
