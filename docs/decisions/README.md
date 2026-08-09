---
schema_version: "1.0"
document_id: VEP-ADR-INDEX
title: "Architecture Decision Records"
document_type: decision-index
status: active
canonical: true
canonical_for: architecture-decision-index
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:07:00+09:00
authors:
  - Xpotato1024
  - OpenAI ChatGPT
owners:
  - Xpotato1024
reviewers: []
source_issue: 1
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-GOV-CHANGE", "VEP-ADR-TEMPLATE", "VEP-ARCH-TECH", "VEP-ADR-0001"]
---
# Architecture Decision Records

## 1. 目的

ADRは、重要な設計判断について、背景、検討した選択肢、採用理由、影響を保存します。

現在の仕様はarchitecture／governance文書へ反映し、ADRは「なぜその仕様になったか」を補足します。

## 2. 状態

- `proposed`
- `accepted`
- `rejected`
- `deprecated`
- `superseded`

## 3. 命名

```text
NNNN-short-kebab-title.md
```

番号は4桁の連番とし、再利用しません。

## 4. 作成条件

- 複数の妥当な選択肢がある。
- 変更コストが高い。
- security、privacy、audit independenceへ影響する。
- 技術基準のrequired／preferredを変更する。
- accepted ADRを覆す。

## 5. 一覧

- [`0001-network-acquisition-credential-and-ephemeral-locator-boundary.md`](0001-network-acquisition-credential-and-ephemeral-locator-boundary.md): accepted

## 6. テンプレート

[`0000-template.md`](0000-template.md) をコピーして使用します。
