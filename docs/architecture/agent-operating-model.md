---
schema_version: "1.0"
document_id: VEP-ARCH-AGENTS
title: "エージェント運用モデル"
document_type: architecture
status: draft
canonical: true
canonical_for: agent-operating-model
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-10T02:00:00+09:00
authors: ["Xpotato1024", "OpenAI ChatGPT"]
owners: ["Xpotato1024"]
reviewers: []
source_issue: 3
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-AGENT-POLICY", "VEP-AGENT-GOV", "VEP-GOV-AI-INSTRUCTIONS", "VEP-ARCH-PIPELINE", "VEP-ARCH-STATE", "VEP-ARCH-ARTIFACTS"]
---
# エージェント運用モデル

## 1. 実行主体

- Deterministic executor: download、hash、FFmpeg、Schema、index、bundle
- Evidence analyst: transcript／frameからevidenceと曖昧点を構築
- Deliverable author: requirement map、outline、draft、claim ledger
- Independent auditor: fresh contextでclaim再抽出、P0／P1／P2
- Reviser: findingとevidenceに限定した修正
- Human-review preparer: 再生確認、差分、未確定点、approval target hash
- Human approver: ユーザー本人。AIへ委譲不可

## 2. Instruction topology

```text
vendor-neutral SoT
  .agents/policy.md
  .agents/governance.md
  .agents/skill-system.toml
        ↓
provider adapters
  AGENTS.md / CLAUDE.md / GEMINI.md / copilot-instructions.md
        ↓
agent execution
```

adapterは自動検出の互換層であり、共通規則を弱めません。

## 3. Skills

active Skillは`repository-sot-audit`のみです。transcript／evidence Schemaと`EVIDENCE_READY`までの
実装が存在しても、`analyze-video-evidence`はdraft／explicit-onlyのままです。現行evalは2つの
expected／forbidden caseだけで、promotion threshold 9に必要なscore、representative run、成果物evidence、
owner reviewを満たす検証はNot Runです。authoring／audit関連Skillも個別evalを完了するまでdraftのままとし、
実装完了だけを理由にimplicit invocationへ昇格しません。

## 4. Authoring contract

入力はvalidated requirements、validated evidence、user notes、output constraintsです。出力は
`draft.md`、`claims.jsonl`、`requirement-map.json`、manifestです。evidence外のvideo fact、
user opinion捏造、timestamp創作を禁止します。

## 5. Audit contract

fixed audit bundleとtarget draftをfresh contextへ渡し、author prompt historyやprivate reasoningを
渡しません。auditorは本文から主張を再抽出し、author claim ledgerは最後に補助比較します。

- P0: 虚偽、逆内容、設問不成立、出典偽装、重大要件欠落
- P1: 重要根拠不足、過剰推論、誤読、timestamp不一致、形式違反
- P2: 表現、冗長性、軽微な構成・明確性

## 6. Revision and escalation

自動revisionは既定2回までです。P0／P1が残る、証拠不足、独立性不足、user opinion未確定の
場合はBLOCKEDとして人間へ戻します。

## 7. Auditability

各AI実行はrole、Skill ID/version、provider/model、context ID、input/output hash、日時、
external API permission、warningsを記録します。private reasoningの保存は要件にしません。
