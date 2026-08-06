---
schema_version: "1.0"
document_id: VEP-AGENTS
title: "AGENTS.md adapter"
document_type: agent-adapter
status: active
canonical: true
canonical_for: agents-md-entrypoint
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors:
  - Xpotato1024
  - OpenAI ChatGPT
owners:
  - Xpotato1024
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-POLICY", "VEP-AGENT-GOV", "VEP-DOC-REGISTRY", "VEP-ARCH-AGENTS"]
---
# AGENTS.md

CANONICAL_AGENT_POLICY: `.agents/policy.md`  
SKILL_REGISTRY: `.agents/skill-system.toml`

このファイルは、`AGENTS.md`を自動検出するAI向けの薄い入口です。詳細規則の正本は
[`.agents/policy.md`](.agents/policy.md)、Skill運用の正本は
[`.agents/governance.md`](.agents/governance.md)です。

作業開始時に、対象Issue／PR、`docs/registry/documents.yaml`、該当canonical文書、
`.agents/policy.md`を確認してください。

必須事項:

- 認可されていない動画を取得しない。
- Cookie、token、署名付きURL、実動画、音声、字幕、抽出フレーム、ユーザー成果物をコミットしない。
- raw sourceを上書きしない。未実行・不明・低確度を推測で補完しない。
- authoringとindependent auditを同一コンテキストで完結させない。
- Issue-first、Draft PR、明示依頼なしのReady化／merge／Issue closeを行わない。
- 文書追加・移動・正本性変更時は文書台帳を同じPRで更新する。
- 変更前後に次を実行する。

```bash
uv sync --locked --all-groups
uv run ruff check scripts tests
uv run ruff format --check scripts tests
uv run pytest -q
uv run python scripts/validate_repository.py --root .
```

Skillは明示的に選択し、`lifecycle = "draft"`のSkillを実運用として実行しないでください。
