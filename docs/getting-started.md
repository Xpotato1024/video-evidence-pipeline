---
schema_version: "1.1"
document_id: VEP-GUIDE-GETTING-STARTED
title: "Getting started"
document_type: guide
status: active
canonical: true
canonical_for: getting-started
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-README
  - VEP-OPS-CLI-LOCAL
  - VEP-OPS-ASR
  - VEP-OPS-AI-EXCHANGE
  - VEP-ARCH-TRUST
---
# Getting started

このガイドは、初めてVEPを使う人が、認可済みの入力から`HUMAN_REVIEW_READY`までを確認するための入口です。詳細なpath・hash・stateの仕様は[CLI and local workspace operations](operations/cli-and-local-workspace.md)、semantic exchangeの契約は[AI exchange operations](operations/ai-exchange.md)を正本とします。

## 1. 用意するもの

- Python 3.12とlock済みの`uv`
- VEPが受け付ける認可済みsource
- local処理を行う権限の明示確認
- semantic responseを作るlocal runner／reviewer、または外部送信を明示認可したexternal provider

最短経路ではUTF-8のsupplied transcriptとlocal runnerを使います。この経路はmedia decode、FFmpeg、ASR modelを必要とせず、semantic requestをcloud AIやexternal APIへ送信しません。実sourceやresponseはrepository外のprivate workspaceに置いてください。

## 2. Transcriptから始める最短経路

まずassignmentの目的と読者を固定します。`--purpose`と`--audience`はsemantic selectionの入力であり、後からAIが推測する値ではありません。

```bash
uv run vep job init transcript-summary \
  --source-kind local_file \
  --source /absolute/path/to/authorized-transcript.txt \
  --primary-input transcript \
  --task-type summary \
  --language ja \
  --purpose "講義の中心命題と復習条件を確認する" \
  --audience "初学者" \
  --requirement "中心命題を保持する" \
  --requirement "sourceにある具体的な条件を保持する" \
  --max-chars 300 \
  --output-format markdown

WORKSPACE=.local/jobs/transcript-summary
uv run vep job authorize "$WORKSPACE" \
  --confirm \
  --basis "authorized local transcript" \
  --allow-local-processing \
  --deny-external-api
uv run vep acquire "$WORKSPACE"
uv run vep normalize "$WORKSPACE"
uv run vep transcribe "$WORKSPACE"
```

`transcript` primaryでは`normalize`はno-media manifestを作り、`transcribe`はsupplied transcriptを検証します。FFmpegを先にインストールしたり、ASR modelをdownloadしたりする必要はありません。

上の`--deny-external-api`はlocal-only経路の契約です。external providerを使う場合は、入力の外部送信が許可されていることを人間が確認し、一度限りの`job authorize`で代わりに`--allow-external-api`を指定してください。denyのまま外部へ送信してはならず、VEPも`external_api_used=true`のresponse importを拒否します。

## 3. Requestを渡してsemantic stageを実行する

各semantic stageは、先にdeterministic executorがrequestを固定します。`prepare --json`の出力を認可条件に合うrunnerへの入力として扱い、ファイル名やSkillを推測しないでください。

| Stage | Prepare | runnerへ渡すもの | Import |
| --- | --- | --- | --- |
| Refinement | `vep refine prepare WORKSPACE --json` | `request_path`、`skill_path`、`response_schema_path` | `vep refine import WORKSPACE --response RESPONSE` |
| Evidence | `vep evidence prepare WORKSPACE --json` | `request_path`、`skill_path`、`response_schema_path` | `vep evidence import WORKSPACE --response RESPONSE` |
| Author | `vep author prepare WORKSPACE --skill draft-video-summary --json` | `request_path`、`skill_path`、`response_schema_path` | `vep author import WORKSPACE --response RESPONSE` |
| Audit | `vep audit prepare WORKSPACE --skill independent-content-audit --json` | `request_path`、`skill_path`、`response_schema_path` | `vep audit import WORKSPACE --response RESPONSE` |
| Revision | `vep revision prepare WORKSPACE --skill revise-from-audit --json` | `request_path`、`skill_path`、`response_schema_path` | `vep revision import WORKSPACE --response RESPONSE` |

明示したprimary inputを使う新規jobでは、raw transcriptをimmutableに保ったままrefinement responseを別artifactとしてimportします。その後にEvidenceを作り、private search cacheを構築します。

```bash
uv run vep refine prepare "$WORKSPACE" --json
uv run vep refine import "$WORKSPACE" \
  --response /absolute/path/to/refinement-response.json
uv run vep evidence prepare "$WORKSPACE" --json
# 表示されたrequest_path + skill_path + response_schema_pathをrunnerへ渡す
uv run vep evidence import "$WORKSPACE" \
  --response /absolute/path/to/evidence-response.json
uv run vep index build "$WORKSPACE"
```

AuthoringではSkill snapshotも同じexchangeへ渡します。

```bash
uv run vep author prepare "$WORKSPACE" \
  --skill draft-video-summary --json
# request_path + skill_path + response_schema_pathを同じrunner invocationへ渡す
uv run vep author import "$WORKSPACE" \
  --response /absolute/path/to/author-response.json
```

Auditはauthorと別のfresh contextで実行し、過去のauthor claimを正解として再利用しません。

```bash
uv run vep audit prepare "$WORKSPACE" \
  --skill independent-content-audit --json
uv run vep audit import "$WORKSPACE" \
  --response /absolute/path/to/audit-response.json
```

auditのresponseがP0／P1 findingを含まなければ、jobは`HUMAN_REVIEW_READY`になります。findingがあれば`REVISION_REQUIRED`で停止し、対応するrevisionだけを作ります。AI runnerはcanonical workspaceへ書き込まず、import後のschema／fingerprint／SourceRef／length検証とartifact公開はVEP executorが行います。

## 4. Video／audio inputへ進む場合

mediaをprimary inputにする場合は、認可、取得、normalizationを先に通します。

```bash
uv run vep job init media-report \
  --source-kind local_file \
  --source /absolute/path/to/authorized-video.mp4 \
  --primary-input video \
  --task-type report \
  --language ja \
  --purpose "講義の主張を根拠付きで確認する" \
  --audience "レビュー担当者" \
  --requirement "主張ごとにevidence refを付ける" \
  --output-format markdown
uv run vep job authorize .local/jobs/media-report --confirm \
  --basis "authorized local video" \
  --allow-local-processing \
  --deny-external-api
uv run vep acquire .local/jobs/media-report
uv run vep normalize .local/jobs/media-report
uv run vep transcribe .local/jobs/media-report
```

caption coverageが十分ならASRは起動しません。不足区間がある場合だけ、[`ASR runtime operations`](operations/asr-runtime.md)のrepository-owned workerが明示的にprovision済みのmodelを使います。未provision modelの暗黙download、explicit CUDA失敗の無断fallback、任意FFmpeg引数の注入は行いません。

## 5. 状態を確認し、transcriptを再利用する

`vep job guide WORKSPACE`はread-onlyの次操作案内です。現在state、次の操作、必要なrequest／Skill／schema path、side effectの分類を表示します。

```bash
uv run vep job guide .local/jobs/media-report
uv run vep job status .local/jobs/media-report
```

assignmentだけを変えるときは、親を編集せずchild jobへtranscript boundaryから分岐します。

```bash
uv run vep job recover .local/jobs/media-report changed-assignment \
  --child-spec /absolute/path/to/changed-assignment.yaml \
  --confirm \
  --basis "reuse the verified transcript under a changed assignment"
```

recoveryはsource、normalization、raw transcript、宣言済みsupporting inputだけを再利用し、childを`TRANSCRIBED`から開始します。既存のdraft／audit／approvalをsemantic shortcutとして引き継がず、新しいevidence、author、auditを作ります。親workspaceの上書き、再取得、不要な再ASRは行いません。

upgrade前にprepare済みで未完了のsemantic requestが現在のfrozen Skill contractへimportできない場合も、同じ境界を使います。`vep job guide`が`migration-required`を返したら古いrequestをpatchせず、表示された`vep job recover ... --child-spec ...`でverified transcriptからchildを作り、semantic stageだけをcurrent Skillで再開します。

## 6. Human reviewとexport

`HUMAN_REVIEW_READY`は承認済みを意味しません。reviewerはbundle内のdraft、evidence、監査、要件、target SHAを確認し、次を明示してからapprovalします。

```bash
uv run vep approve .local/jobs/transcript-summary \
  --approval-target-sha256 <reviewed-draft-sha256> \
  --reviewer "local-reviewer" \
  --basis "reviewed the human-review package" \
  --confirm
uv run vep export .local/jobs/transcript-summary
```

AIやSkillはこのapproval経路へ入れません。reviewでP0／P1が残る場合、approvalとexportは停止します。

## 関連資料

- [README](../README.md): 初見向けの全体像、pipeline図、source matrix
- [AI exchange operations](operations/ai-exchange.md): semantic request／Skill／schemaの詳細
- [Semantic quality](concepts/semantic-quality.md): selection before compressionと研究source
- [CLI and local workspace operations](operations/cli-and-local-workspace.md): state、recovery、workspace保護
- [Network acquisition operations](operations/network-acquisition.md): network authorizationとlocator境界
