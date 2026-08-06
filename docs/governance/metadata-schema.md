---
schema_version: "1.0"
document_id: VEP-GOV-METADATA
title: "文書メタデータSchema"
document_type: governance
status: active
canonical: true
canonical_for: document-metadata-schema
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
related_documents: ["VEP-GOV-DOCUMENTS", "VEP-GOV-CHANGE", "VEP-DOC-REGISTRY"]
---
# 文書メタデータSchema

## 1. 形式

Markdown文書は先頭にYAML front matterを持ちます。日時はISO 8601形式でタイムゾーンを明示します。

```yaml
---
schema_version: "1.0"
document_id: VEP-ARCH-EXAMPLE
title: "例示文書"
document_type: architecture
status: draft
canonical: true
canonical_for: example-responsibility
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
related_documents:
  - VEP-GOV-DOCUMENTS
---
```

## 2. 必須field

| Field | 型 | 制約 |
|---|---|---|
| `schema_version` | string | 現在は`"1.0"` |
| `document_id` | string | リポジトリ内で一意、不変 |
| `title` | string | 人間向けの文書名 |
| `document_type` | string | 文書分類 |
| `status` | enum | `draft`, `active`, `deprecated`, `historical`, `template` |
| `canonical` | boolean | 現在の正本か |
| `canonical_for` | string/null | 正本責務。historicalではnull |
| `created_at` | datetime | 初回作成日時。後から変更しない |
| `updated_at` | datetime | 最終の意味的変更日時 |
| `authors` | string[] | 初稿・重要変更の著者 |
| `owners` | string[] | 維持責任者 |
| `reviewers` | string[] | 内容を明示的にレビューした主体 |
| `source_issue` | integer/null | 作成・主要改訂の起点Issue |
| `change_control` | enum | 原則`pull-request` |
| `supersedes` | string[] | この文書が置換する文書ID |
| `superseded_by` | string/null | この文書を置換した文書ID |
| `related_documents` | string[] | 主要関連文書ID |

## 3. Fieldの意味

### `created_at`

文書の初回作成日時です。ファイル移動、名称変更、historical化後も維持します。Git commit日時で代用しません。

### `updated_at`

意味、責務、規則、設計判断が変わった時に更新します。誤字、空白、リンク修正だけの場合は更新しなくても構いません。ただし、判断に迷う場合は更新します。

### `authors`

文書の内容を作成した主体を記録します。AIが実質的に初稿または改訂を作成した場合は、利用したAIを含めます。単なるformat実行やlintは著者に含めません。

### `owners`

現在の維持責任者です。著者と一致する必要はありません。

### `reviewers`

内容を確認した主体のみ記録します。PRを作成しただけ、AIが自己監査しただけ、人間が未確認である場合は追加しません。

### `canonical_for`

文書が何の正本かを表す安定したslugです。pathやtitleではなく責務を表します。

### `supersedes`／`superseded_by`

重要な設計の置換関係を文書IDで表します。Git commit SHAは補助情報であり、文書IDの代替ではありません。

## 4. 条件付きfield

Historical文書では次を追加します。

```yaml
historical_reason: "パイプラインv2への移行により置換"
historical_at: 2026-10-01T12:00:00+09:00
```

Generated文書では次を追加できます。

```yaml
generated_from:
  - schemas/job.schema.json
generator: v2r-docgen
```

定期レビュー対象では次を追加できます。

```yaml
last_reviewed_at: 2026-08-06T16:07:00+09:00
review_interval_days: 180
```

## 5. 整合性規則

- `canonical: true` なら `canonical_for` は非null
- `canonical: false` かつ `status: historical` なら `canonical_for` はnull
- `status: historical` なら `superseded_by` と`historical_reason`が必要
- `created_at <= updated_at`
- `document_id`、`supersedes`、`superseded_by`、`related_documents` は台帳上のIDを参照する
- 同一`canonical_for`に複数のcanonical文書を認めない
- `reviewers`にAIを追加する場合、独立した監査コンテキストで実施した事実を確認できること
- human reviewを受けていない文書に人間をreviewerとして追加しない

## 6. 将来の機械検証

後続実装では、Markdown front matterと`documents.yaml`を読み取り、次をCIで検証します。

- 必須field
- 日時形式
- 文書ID一意性
- path一致
- 参照先存在
- canonical責務の一意性
- historical条件
- Git管理禁止ファイル
