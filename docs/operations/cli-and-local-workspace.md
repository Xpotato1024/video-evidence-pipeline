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

`job init`ではassignment requirementをrepeatableな`--requirement`で指定します。未指定時は空配列で、
指定順は意味を持ち、normalized job fingerprintへそのまま含まれます。空白だけの値と512文字を超える
値は拒否します。

```bash
vep job init demo-report \
  --source-kind local_file \
  --source /path/to/authorized-source.mp4 \
  --task-type report \
  --language ja \
  --requirement "cite two concrete observations" \
  --requirement "separate facts from inference" \
  --output-format markdown
```

## Authorization flow

`job init`は`CREATED`を記録するだけで、sourceを読みません。`job authorize`は`--confirm`を必須とし、
authorization basis、local processing可否、external API可否、確認時刻、確認者identity、normalized
job fingerprintを保存します。external APIは既定denyです。

authorization後に`job.yaml`の意味内容が変わると`AUTHORIZATION_STALE`になり、`acquire`は失敗します。
CLIは再認可を推測しません。このMVPではin-place再認可も実装せず、元の認可済みjob内容へ戻すか、
新しいjobを作成します。

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

新規runtime workspaceはprivate-by-defaultです。POSIXでは`.local/`以下の全directoryを`0700`、
job YAML、authorization、manifest、transition log、raw artifact、temporary／lock fileを`0600`へ
作成時に設定・検証し、適用できなければ処理を失敗させます。final targetもtemporary fileと同じ
private modeを維持します。WindowsではPOSIX modeをsecurity guaranteeとして扱わず、同一ユーザーの
OS account／NTFS ACL境界に依存します。本MVPはWindows ACLの再構成機能を実装しません。

## Local sourceとidempotency

`local_file` adapterはregular、non-zero fileだけを受け付け、streaming SHA-256後にcontent-addressed
pathへexclusive copyし、copy後のsize／hashを再検証します。同一job、同一normalized job、同一source
contentの再実行は既存raw artifactとmanifestを検証して再利用し、transitionを重複追加しません。
同一jobのacquisitionでは、probe／copy／manifest公開／`ACQUIRED` transitionをjob-level排他境界内で
直列化します。短いlock取得猶予後も競合中なら`TRANSITION_LOG_BUSY`でfail closedし、別attemptのmanifestを
混在させません。排他はOS file lockで保持し、process異常終了時はkernelによって解放されるためretryできます。

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
- `AUTHORIZATION_STALE`: current `job.yaml`を認可時の内容へ戻すか、新しいjobを作成する。
- `SOURCE_CONTENT_CHANGED`: 既存jobを変更せず、新しいjobを作る。
- `ARTIFACT_INTEGRITY_FAILED`: raw artifactを成功扱いせず、workspaceを保全して原因を調査する。
- `SOURCE_KIND_NOT_IMPLEMENTED`: `local_file`以外は後続Issueの実装を待つ。

FFmpeg、subtitle、frame、ASR、evidence、authoring、independent audit、approval、exportは本運用範囲外です。
