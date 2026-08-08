---
schema_version: "1.1"
document_id: VEP-ARCH-ARTIFACTS
title: "成果物モデル"
document_type: architecture
status: draft
canonical: true
canonical_for: artifact-model
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
related_documents: ["VEP-ARCH-PIPELINE", "VEP-ARCH-STATE", "VEP-ARCH-TRUST"]
---
# 成果物モデル

## 1. 原則

- raw artifactはimmutable
- 派生成果物は入力へ遡れる
- human-facing Markdownとmachine-facing JSON／JSONLを分離
- search indexは再生成可能なcache
- finalはhuman-approved draftからのみ生成

## 2. Artifact class

| Class | 例 | Mutable | Git管理 |
|---|---|---:|---:|
| source | original video, original captions | No | No |
| normalized | analysis video, speech audio, normalized subtitles | Regenerate | No |
| observation | transcript segment, frame record | Append／regenerate | No |
| evidence | evidence ledger, ambiguity ledger | Versioned | No |
| authoring | outline, draft, claims, requirement map | Versioned | 原則No |
| audit | audit claims, findings, audit report | Versioned | 原則No |
| approval | approval record | Append-only | No |
| export | DOCX, PDF, HTML | Regenerate | No |
| definition | code, Schema, templates, Skills | PR controlled | Yes |
| synthetic fixture | artificial test media | PR controlled | Yes |

## 3. Lineage envelope

すべての派生成果物は、少なくとも次を追跡できる必要があります。

実装済みの`ArtifactRef`、`ArtifactRecord`、`ProducerIdentity`は、artifact ID／type、content
SHA-256、size、workspace-relative path、input artifact、producer kind／name／version、normalized
configuration fingerprint、timezone-aware timestamp、warnings、redacted errorをstrict modelとして
保持します。JSON SchemaはPydantic modelから生成します。

```json
{
  "schema_version": "1.0",
  "artifact": {
    "artifact_id": "source-0123456789abcdef",
    "artifact_type": "raw_source",
    "content_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "size_bytes": 123,
    "relative_storage_path": "source/raw/0123456789abcdef.../synthetic.bin"
  },
  "input_artifacts": [],
  "producer": {
    "kind": "adapter",
    "name": "local_file",
    "version": "0.1.0"
  },
  "configuration_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
  "created_at": "2026-08-07T00:00:00Z",
  "warnings": [],
  "redacted_error": null
}
```

AI生成物ではmodel、実行形態、Skill version、入力bundle hashを記録します。秘密情報や生の認証headerはlineageへ含めません。

## 4. Evidence record

```json
{
  "evidence_id": "E0042",
  "topic": "PID制御の限界",
  "observation": "講師はゲイン増加だけでは振動を抑えられないと説明した",
  "source_refs": [
    {"type": "transcript", "id": "T001284"},
    {"type": "frame", "id": "F000127"}
  ],
  "confidence": "high",
  "interpretation_level": "explicit_statement",
  "review_status": "unreviewed"
}
```

`interpretation_level`:

- `verbatim`
- `explicit_statement`
- `visual_observation`
- `reasonable_inference`
- `speculation`
- `user_opinion`
- `external_fact`

`speculation`は動画内事実として成果物へ使用できません。使用する場合は仮説であることを本文に明示します。

## 5. Claim record

```json
{
  "claim_id": "C0017",
  "draft_span": {"paragraph": 4, "sentence": 2},
  "text": "講師は単純なゲイン増加では安定性が保証されないと説明している。",
  "claim_type": "video_fact",
  "evidence_ids": ["E0042"],
  "author_confidence": "high"
}
```

claim type:

- `video_fact`
- `visual_fact`
- `inference`
- `user_opinion`
- `external_fact`
- `transition_or_rhetoric`

監査者はauthor claim recordだけに依存せず、本文からclaimを再抽出します。

## 6. Manifest

各stageはmanifestを持ちます。

- stage
- state transition
- input/output artifact
- hashes
- tool/model versions
- parameters fingerprint
- started/finished time
- success/failure
- warnings
- redacted error summary

## 7. Storage layout

`ACQUIRED`までの実装済みローカル配置:

```text
.local/jobs/<job-id>/
├─ job.yaml
├─ authorization/
│  └─ authorization.json
├─ source/
│  └─ raw/
│     └─ <sha256>/
│        └─ <sanitized-original-name>
├─ manifests/
│  ├─ source-manifest.json
│  ├─ transitions.jsonl
│  └─ stages/
│     └─ acquisition.json
└─ logs/
```

pathは識別子ではありません。artifact IDとhashを正本とします。
`normalized/`以降の配置は、それぞれのstage実装時に追加します。

## 8. Approval binding

approval recordは次を含みます。

- approved draft path
- approved content SHA-256
- reviewer identity
- approved_at
- unresolved notes

export時にdraft hashが一致しなければ失敗させます。
