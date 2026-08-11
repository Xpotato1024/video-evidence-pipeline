---
schema_version: "1.1"
document_id: VEP-ARCH-ARTIFACTS
title: "成果物モデル"
document_type: architecture
status: draft
canonical: true
canonical_for: artifact-model
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-10T02:00:00+09:00
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
related_documents: ["VEP-ARCH-PIPELINE", "VEP-ARCH-STATE", "VEP-ARCH-TRUST", "VEP-OPS-NETWORK"]
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

transcript JSONL、validated analyzer response、evidence JSONL、ambiguity ledgerはcanonical artifactです。
`search/index.sqlite3`はcanonical JSON／JSONLから再構築するprivate cacheで、manifestのoutput artifactや
state transition根拠には含めません。

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

network sourceの`ProbeResult`はmetadata-onlyを許容し、declared size、content type、SHA-256は未確定でも
構いません。actual SHA-256とsizeはprivate stagingへ取得した実bytesから確定し、ETag、Content-MD5、
Box SHA-1等で代用しません。

## 4. Evidence record

```json
{
  "schema_version": "1.0",
  "evidence_id": "E000042",
  "observation": "講師はゲイン増加だけでは振動を抑えられないと説明した",
  "source_refs": [
    {
      "kind": "transcript_segment",
      "ref_id": "T001284",
      "start_ms": 128400,
      "end_ms": 131200,
      "content_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ],
  "confidence": "high",
  "interpretation_level": "explicit_statement",
  "review_status": "unreviewed",
  "asserted_as_video_fact": true,
  "flags": []
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

`verbatim`／`explicit_statement`はtranscript ref、`visual_observation`はframe ref、`external_fact`は
requestに列挙された`approved_artifact` refを必要とします。`reasonable_inference`、`speculation`、
`user_opinion`、`external_fact`を動画内事実としてassertできません。semantic analyzerは`unreviewed`だけを
出力でき、人間review recordなしに自己承認できません。

## 5. Claim record

```json
{
  "claim_id": "C000017",
  "draft_span": {
    "start": 0,
    "end": 31,
    "text": "講師は単純なゲイン増加では安定性が保証されないと説明している。"
  },
  "text": "講師は単純なゲイン増加では安定性が保証されないと説明している。",
  "claim_type": "video_fact",
  "evidence_ids": ["E000042"],
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

network source manifestにはtyped redacted acquisition evidenceを追加し、adapter、transport／backend／version、
取得時刻、requested／final host、redirect count、HTTP status category、content type、declared length、actual size、
credential mode、warningsを保持します。full URL、query、redirect Location、token、Cookie、credential path、
生のyt-dlp出力は保持しません。
backendが観測できないfinal hostとredirect countは`null`とし、推測値へ置き換えません。

yt-dlp取得は`progressive`または`adaptive-merged`を記録します。selected formatごとにformat ID、video codec、
audio codec、protocolを保持し、adaptive mergeではstream-copyに使用したFFmpegのversion／executable SHA-256／
capabilitiesをmerger identityとして保持します。componentのremote URLとsigned URLは保持しません。
`ArtifactRecord`のSHA-256／sizeは統合後final artifactのidentityであり、remote originalや各componentのhashを
表すものではありません。統合前componentを単一remote originalとして扱いません。

## 7. Storage layout

`HUMAN_REVIEW_READY`／`BLOCKED`までの実装済みローカル配置:

```text
.local/jobs/<job-id>/
├─ job.yaml
├─ authorization/
│  └─ authorization.json
├─ source/
│  ├─ staging/                  # network取得中だけ使用するprivate staging
│  └─ raw/
│     └─ <sha256>/
│        └─ <sanitized-original-name>
├─ normalized/
│  ├─ staging/                 # FFmpeg処理中だけ使用するprivate attempt
│  ├─ analysis/<sha256>/analysis.mp4
│  ├─ audio/<sha256>/speech.wav
│  ├─ subtitles/<sha256>/subtitle-<stream>.{vtt,jsonl}
│  ├─ frames/<sha256>/F<id>.png
│  └─ index/<sha256>/frame-index.json
├─ transcript/
│  ├─ staging/
│  └─ segments/<sha256>/transcript.jsonl
├─ evidence/
│  ├─ staging/
│  ├─ request.json
│  ├─ responses/<sha256>/response.json
│  ├─ records/<sha256>/evidence.jsonl
│  └─ ambiguities/<sha256>/ambiguities.json
├─ search/
│  ├─ staging/
│  └─ index.sqlite3               # private／rebuildable／non-canonical
├─ semantic/
│  └─ staging/                     # authoring／audit／revisionのprivate attempt
├─ authoring/
│  ├─ requests/{author,audit,revision}-vNNN.json
│  ├─ responses/<sha256>/{author,audit,revision}-response.json
│  ├─ drafts/vNNN/{draft.md,claims.jsonl,requirement-map.json,agent-run.json}
│  ├─ audits/vNNN/{audit-claims.jsonl,findings.json,claim-comparison.jsonl,agent-run.json}
│  └─ revisions/vNNN/resolutions.jsonl
├─ human-review/vNNN/bundle.json       # review package; approval recordではない
├─ approval/records.jsonl              # private append-only human approval ledger
├─ delivery/staging/                    # Markdown export中だけ使用するprivate attempt
├─ exports/
│  └─ markdown/<approval-record-sha256>/{final.md,package-manifest.json}
├─ manifests/
│  ├─ source-manifest.json
│  ├─ media-manifest.json
│  ├─ transcript-manifest.json
│  ├─ evidence-manifest.json
│  ├─ authoring/{draft-vNNN,audit-vNNN,revision-vNNN,human-review}.json
│  ├─ approval-manifest.json
│  ├─ export-manifest.json
│  ├─ transitions.jsonl
│  └─ stages/
│     ├─ acquisition.json
│     ├─ normalization.json
│     ├─ transcription.json
│     ├─ evidence.json
│     └─ {authoring,audit,revision,human-review}-vNNN.json, approval.json, export.json
└─ logs/
```

pathは識別子ではありません。artifact IDとhashを正本とします。
normalized artifactもrawと同様にcontent-addressed pathへ排他的に公開し、raw sourceを上書きしません。
transcript segmentの`raw_text`は選択caption cueまたはASR backend出力を変形せず保持し、
`normalized_text`はNFKC＋空白正規化した検索／比較用文字列です。normalized subtitle cue自体はFFmpeg
WebVTT decode後のtextなので、元subtitle markupのraw preservationとは区別します。
`evidence.jsonl`だけは支持可能なrecordが0件の場合にzero-line／zero-byteをcanonical表現とします。
raw、normalized、transcript、validated response、ambiguity ledgerは引き続きnon-zero artifactです。
draftは`v001`、revisionは`v002`／`v003`をcontent hashとpredecessor lineageで識別し、pathやversion名だけを
identityにしません。audit requestはauthor claim／run／contextを含まない独立入力で、claim comparisonはvalidated
audit import後に生成する別artifactです。human-review bundleはcurrent draft、audit findings、comparison、requirement
map、revision history、approval target hashを束縛しますが、approval recordではありません。

## 8. Approval binding

approval recordは次を含みます。

- job／job fingerprintと連続sequence
- human-review bundle ArtifactRef／fingerprintとcurrent audit findings ArtifactRef／bundle fingerprint
- approved draft version／ArtifactRef／content SHA-256
- CLIでassertされたreviewer identityとそのassurance
- approval basis、approved_at、unresolved notes
- intent fingerprint、previous record SHA-256、record self SHA-256

approval ledgerはprivate／newline終端のappend-only JSONLで、v1 runtimeはstate graphに合わせて1 recordだけを許可します。
record append後の停止はrecord自身をdurable markerとしてmanifestを再構築し、過去recordを上書きしません。
reviewer identityは人間がCLIへ申告したlineageであり、OSまたは外部identity providerによる本人確認を意味しません。

Markdown export artifactはapproved draftとbyte-for-byte同一で、package／primary manifestがapproval record、draft、
human-review、audit、fixed evidence、adapter identity、export ArtifactRefを束縛します。export時にいずれかのhashが
一致しなければ`APPROVAL_STALE`またはintegrity errorで失敗させます。
