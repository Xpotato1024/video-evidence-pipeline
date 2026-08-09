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
  - VEP-OPS-NETWORK
---
# CLI・local workspace運用

## 実装範囲

`vep`は現在、job作成、validation、明示authorization、local／network acquisition、media normalization、
status／integrity確認、JSON Schema生成／drift確認を実装しています。実行可能なstate pathは
`CREATED → AUTHORIZED → ACQUIRED → NORMALIZED`です。

## Commands

```text
vep --version
vep job init
vep job validate
vep job authorize
vep job status
vep acquire
vep normalize
vep schema export
vep schema check
```

`validate`、`status`、`acquire`、`normalize`は`--json`で機械可読出力を返します。通常の契約違反はstableな
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
authorization basis、local processing可否、network acquisition可否、external API可否、確認時刻、確認者identity、
normalized job fingerprintを保存します。network acquisitionとexternal APIは別permissionで、どちらも既定denyです。
同一jobへのauthorizationはacquisitionと同じjob-level OS file lockで直列化します。stateをlock取得後に
再読込し、authorization recordのatomic writeと`CREATED → AUTHORIZED` transitionを同じ排他境界内で
実行します。競合callerは`TRANSITION_LOG_BUSY`またはstate gateでfail closedし、loserのrecordを残しません。
process異常終了でstateが`CREATED`のままauthorization recordだけ残った場合、kernel lock解放後のretryで
recordを置換してtransitionを完了できます。

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
├─ normalized/
│  ├─ staging/
│  ├─ analysis/<sha256>/analysis.mp4
│  ├─ audio/<sha256>/speech.wav
│  ├─ subtitles/<sha256>/subtitle-<stream>.{vtt,jsonl}
│  ├─ frames/<sha256>/F<id>.png
│  └─ index/<sha256>/frame-index.json
├─ manifests/
│  ├─ source-manifest.json
│  ├─ media-manifest.json
│  ├─ transitions.jsonl
│  └─ stages/
│     ├─ acquisition.json
│     └─ normalization.json
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

## Network source

network jobは`job authorize --allow-network-acquisition`を明示します。tokenは`vep acquire`の
`--box-token-env`／`--panopto-token-env`で環境変数名だけを指定します。Panopto tokenにはさらに
`--panopto-tenant-host`で送信先tenantを拘束します。browser cookieは
`--cookies-from-browser`で明示します。timeoutとmaximum bytesは`--network-timeout`、
`--maximum-download-bytes`、`--maximum-download-seconds`でboundedに設定できます。詳細なlocator、redirect、SSRF、provider、idempotencyの
契約は[`network-acquisition.md`](network-acquisition.md)を参照してください。

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
- `NETWORK_ACQUISITION_NOT_AUTHORIZED`: network取得を明示許可した新jobを用意する。
- `YTDLP_NOT_AVAILABLE`: lock済みdependencyを再同期する。
- `PANOPTO_CREDENTIAL_REQUIRED`／`BOX_CREDENTIAL_REQUIRED`: token valueではなく環境変数名を指定する。

## Media normalization

`vep normalize JOB [--json]`は検証済み`ACQUIRED` raw artifactだけを入力とし、任意のFFmpeg引数を
受け付けません。analysis proxy、必要時の`speech.wav`、embedded text subtitleのWebVTT／normalized decoded cue JSONL、
periodic／scene-change frameとframe indexをprivate stagingで検証後、content-addressed pathへ排他的に公開し、
`NORMALIZED`へ遷移します。audioまたはsubtitleがないmediaは成功し、audioなしは
`AUDIO_STREAM_UNAVAILABLE` warningを記録します。normalizable-text allowlistは`subrip`／`srt`／`text`／
`webvtt`だけで、ASS、SSA、`mov_text`、bitmap、unknown codecは変換成功とせずunsupportedとして記録します。cue
JSONLはFFmpegのWebVTT transcodeから得たnormalized decoded cue textであり、`raw_text_preserved`は常にfalseです。
raw media bytesはimmutableですが、SRT／WebVTT presentationやcontrol markupを含むcue textのexact preservationは約束しません。

FFmpeg処理中はtransition用job lockを保持しませんが、normalization専用lockはoperation全体で保持します。これにより
active attemptがstale cleanupで削除されず、hard crash後のstale attemptだけを同lock下でdirect childとして検証・削除します。
link、junction、mount point、traversal形状は削除せず拒否します。全local inputにはconfigの`-max_streams`、
`-max_pixels`、`-max_samples`をprobe／decode前に指定し、両toolのgeneric decoder-limit capabilityを確認します。
tool executableはcapability discoveryの両edge、pin後の各subprocess起動直前／終了直後、publish直前にSHA-256と
stable metadataで再検証するためpersistentまたは偶発的replacementは拒否します。same-userがhash境界内だけ置換して
復元するtransient raceはcovered threatではなく、tool installationはtrusted local-admin boundaryです。各outputとaggregate staging budgetを生成中に確認し、subtitle full readは
per-subtitle上限を`stat`で確認してから実行します。frame indexはseek要求値ではなくobserved FFmpeg PTSを保存し、
要求candidateは理由とともに別フィールドへ残します。処理前snapshotと公開直前のstate／raw hashをlock下で再検証し、
競合bundleのfingerprintが異なれば拒否します。decoder slotのdefaultはvideo 34,070,528、audio 1,048,576で、
configでもこのhard capを超えられません。完全bundleの再実行、`job validate`、`job status`は保存済みtool identity込み
fingerprint、全artifact hash、media manifest、normalization stage manifestを検証し、subprocessなしで再利用します。
partial manifestは検証可能な完全artifactがある場合だけ復旧します。

ASR、evidence、authoring、independent audit、approval、exportは本運用範囲外です。
