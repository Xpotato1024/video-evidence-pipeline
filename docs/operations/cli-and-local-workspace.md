---
schema_version: "1.1"
document_id: VEP-OPS-CLI-LOCAL
title: "CLI・local workspace運用"
document_type: operations
status: draft
canonical: true
canonical_for: cli-local-workspace-operations
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-ARCH-PIPELINE
  - VEP-ARCH-ARTIFACTS
  - VEP-ARCH-STATE
  - VEP-ARCH-TECH
  - VEP-ARCH-TRUST
  - VEP-OPS-CI
---
# CLI・local workspace運用

## 実装範囲

`vep`は現在、job作成、validation、明示authorization、`local_file` acquisition、status／integrity
確認、JSON Schema生成／drift確認を実装しています。実行可能なstate pathは
`CREATED → AUTHORIZED → ACQUIRED`です。

## Commands

```text
vep --version
vep job init
vep job validate
vep job authorize
vep job status
vep acquire
vep schema export
vep schema check
```

`validate`、`status`、`acquire`は`--json`で機械可読出力を返します。通常の契約違反はstableな
error codeとsafe messageを返し、Python stack traceを表示しません。

## Authorization flow

`job init`は`CREATED`を記録するだけで、sourceを読みません。`job authorize`は`--confirm`を必須とし、
authorization basis、local processing可否、external API可否、確認時刻、確認者identity、normalized
job fingerprintを保存します。external APIは既定denyです。

authorization後に`job.yaml`の意味内容が変わると`AUTHORIZATION_STALE`になり、`acquire`は失敗します。
CLIは再認可を推測しません。

## Workspace layout

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

workspace-relative pathはtraversal、absolute化、link／junction経由のworkspace外書込みを拒否します。
JSON、YAML、manifestはatomic writeし、transition logはnewline終端のappend-only JSONLです。

## Local sourceとidempotency

`local_file` adapterはregular、non-zero fileだけを受け付け、streaming SHA-256後にcontent-addressed
pathへexclusive copyし、copy後のsize／hashを再検証します。同一job、同一normalized job、同一source
contentの再実行は既存raw artifactとmanifestを検証して再利用し、transitionを重複追加しません。

同じlocatorのcontentが変わった場合は既存raw sourceを上書きせず`SOURCE_CONTENT_CHANGED`で停止します。
新しいjob、または将来の明示invalidation workflowが必要です。

source manifestと通常出力にはlocal absolute path、URL query、Cookie、token、Authorization headerを
記録しません。local pathはGit管理外の`job.yaml`だけに保持できます。

## Exit code

| Code | Category |
|---:|---|
| 0 | success |
| 2 | configuration／validation／未実装source kind |
| 3 | authorization／state gate |
| 4 | I/O／tool execution |
| 5 | integrity／immutability violation |

## Troubleshooting

- `AUTHORIZATION_CONFIRMATION_REQUIRED`: `--confirm`を付け、basisとprocessing permissionを確認する。
- `AUTHORIZATION_STALE`: current `job.yaml`を確認し、明示的に再認可する。
- `SOURCE_CONTENT_CHANGED`: 既存jobを変更せず、新しいjobを作る。
- `ARTIFACT_INTEGRITY_FAILED`: raw artifactを成功扱いせず、workspaceを保全して原因を調査する。
- `SOURCE_KIND_NOT_IMPLEMENTED`: `local_file`以外は後続Issueの実装を待つ。

FFmpeg、subtitle、frame、ASR、evidence、authoring、independent audit、approval、exportは本運用範囲外です。
