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

active Skillは`repository-sot-audit`に加え、`draft-video-report`、`draft-video-summary`、
`independent-content-audit`、`revise-from-audit`です。後者4つは明示選択専用
（`automatic_trigger = false`）かつ`instruction-only`で、各result evidenceはSkill本文SHA、eval定義SHA、
評価対象commit、10/10（threshold 9）、P0/P1=0、owner reviewへ束縛されています。`analyze-video-evidence`と
`prepare-human-review`はdraft／explicit-onlyのままで、production importには使用できません。
authoring／audit／revisionの`external_exchange` public response importは`lifecycle = "active"`を必須とし、
draft Skillを`DRAFT_SKILL_FORBIDDEN`で拒否します。fixture executionはrepository test内部だけで許可し、
CLIのproduction routeには公開しません。
requestへ固定したSkill snapshotは履歴の正本であり、完成済みjobを現在のSkill file／eval／lifecycleへ再結合しません。
完成済みのauthor／audit／revision importを同一responseで再実行する場合も、保存済みrequestとcanonical responseを
完全検証してから再利用し、現在のSkill eligibilityを再評価しません。
pending importでは現在snapshotのcontent／eval／identityが固定値と一致し、lifecycleだけが`active`へ昇格した場合を
許可します。それ以外のdriftは`SKILL_SNAPSHOT_STALE`です。

## 4. Authoring contract

入力はvalidated requirements、validated evidence、user notes、output constraintsです。出力は
`draft.md`、`claims.jsonl`、`requirement-map.json`、manifestです。evidence外のvideo fact、
user opinion捏造、timestamp創作を禁止します。

## 5. Audit contract

fixed audit bundleとtarget draftをfresh contextへ渡し、author prompt historyやprivate reasoningを
渡しません。auditorは本文から主張を再抽出し、author claim ledgerは最後に補助比較します。
物理audit requestにはauthor claim／run／contextを含めず、validated response import後に決定的comparison artifactを
生成します。各auditは過去のauthor／reviser／auditor contextをすべて避けます。provider／model／context IDは
provider-neutralな自己申告lineageであり、外部provider側のfreshnessを暗号学的に証明するものではありません。

- P0: 虚偽、逆内容、設問不成立、出典偽装、重大要件欠落
- P1: 重要根拠不足、過剰推論、誤読、timestamp不一致、形式違反
- P2: 表現、冗長性、軽微な構成・明確性

## 6. Revision and escalation

自動revisionは既定2回までです。P0／P1が残る、証拠不足、独立性不足、user opinion未確定の
場合はBLOCKEDとして人間へ戻します。
clean auditではdraft、findings、claim comparison、requirement map、revision history、再生確認SourceRefを
human-review packageへ整理します。このpackageは`is_approval: false`で、人間承認を代行しません。

## 7. Auditability

各AI実行はrole、Skill ID/version、provider/model、context ID、input/output hash、日時、
external API permission、warningsを記録します。private reasoningの保存は要件にしません。
