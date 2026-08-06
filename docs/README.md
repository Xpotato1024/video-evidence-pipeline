---
schema_version: "1.0"
document_id: VEP-DOCS-INDEX
title: "設計ドキュメント索引"
document_type: documentation-index
status: active
canonical: true
canonical_for: documentation-index
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:28:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-GOV-DOCUMENTS", "VEP-DOC-REGISTRY", "VEP-README", "VEP-AGENT-INDEX", "VEP-OPS-CI"]
---
# 設計ドキュメント索引

機械可読な正本一覧は[`registry/documents.yaml`](registry/documents.yaml)です。索引と台帳が
矛盾した場合は変更を停止し、同じPRで整合させます。

## Governance

- [`governance/document-governance.md`](governance/document-governance.md): canonical／historical
- [`governance/metadata-schema.md`](governance/metadata-schema.md): document metadata
- [`governance/change-control.md`](governance/change-control.md): 作成・更新・置換
- [`governance/ai-instruction-governance.md`](governance/ai-instruction-governance.md): AI adapter／Skill SoT

## Architecture

- [`architecture/system-context.md`](architecture/system-context.md)
- [`architecture/pipeline-architecture.md`](architecture/pipeline-architecture.md)
- [`architecture/artifact-model.md`](architecture/artifact-model.md)
- [`architecture/state-machine.md`](architecture/state-machine.md)
- [`architecture/trust-boundaries.md`](architecture/trust-boundaries.md)
- [`architecture/technology-baseline.md`](architecture/technology-baseline.md)
- [`architecture/agent-operating-model.md`](architecture/agent-operating-model.md)

## Operations

- [`operations/ci-quality-gates.md`](operations/ci-quality-gates.md)

## Agent system

- [`.agents/README.md`](../.agents/README.md)
- [`.agents/policy.md`](../.agents/policy.md)
- [`.agents/governance.md`](../.agents/governance.md)
- [`.agents/skill-system.toml`](../.agents/skill-system.toml)

## Decisions and history

- [`decisions/README.md`](decisions/README.md)
- [`decisions/0000-template.md`](decisions/0000-template.md)
- [`historical/README.md`](historical/README.md)

## Templates

- [`templates/canonical-document.md`](templates/canonical-document.md)
- [`templates/historical-document.md`](templates/historical-document.md)

`status: active`は文書運用上の正本を示し、ドメイン実装済みを意味しません。
