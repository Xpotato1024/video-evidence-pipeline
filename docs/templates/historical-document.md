---
schema_version: "1.1"
document_id: VEP-TEMPLATE-HISTORICAL
title: "Historical文書テンプレート"
document_type: document-template
status: template
canonical: true
canonical_for: historical-document-template
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-GOV-DOCUMENTS
  - VEP-GOV-METADATA
  - VEP-GOV-CHANGE
  - VEP-HISTORICAL-INDEX
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

置換先が存在せず責務そのものを廃止する場合は、`superseded_by: null`とし、
`historical_reason`に廃止であることを明示します。

本文はhistorical化時点の内容を保持し、現在の設計へ追従して書き換えません。リンク切れ等の
保守が必要な場合は、historical本文を改変するのではなく注記を追加します。
