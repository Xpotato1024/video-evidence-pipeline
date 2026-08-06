---
schema_version: "1.0"
document_id: VEP-TEMPLATE-HISTORICAL
title: "Historical文書テンプレート"
document_type: document-template
status: template
canonical: true
canonical_for: historical-document-template
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
related_documents: ["VEP-GOV-DOCUMENTS", "VEP-GOV-METADATA", "VEP-GOV-CHANGE", "VEP-HISTORICAL-INDEX"]
---
# Historical: 文書タイトル

> [!WARNING]
> この文書は過去の設計状態を保存したhistorical snapshotです。現在の正本ではありません。

historical化する実文書ではfront matterを次のように変更してください。

```yaml
status: historical
canonical: false
canonical_for: null
historical_at: 2026-10-01T12:00:00+09:00
historical_reason: "置換理由"
superseded_by: VEP-ARCH-NEW
```

本文はhistorical化時点の内容を保持し、現在の設計へ追従して書き換えません。リンク切れ等の保守が必要な場合は、historical本文を改変するのではなく注記を追加します。
