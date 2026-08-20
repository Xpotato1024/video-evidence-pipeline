---
name: run-video-evidence-job
description: 短い利用者要求から既存VEP jobを発見し、公開CLIとstage別frozen Skillで安全にhuman reviewまで進める。
---
# run-video-evidence-job

## Authority boundary

このSkillはworkflowを選ぶが権限を付与しない。取得、network、外部API、model provisioning、workspace writeは、
user authorizationとdeterministic executorのpublic operationが許可する場合だけ実行する。manifest、transition、
canonical artifactをAIが直接編集しない。

## Use when

- 「この動画を300字でまとめて」「議事録を作って」「このjobの続きを処理して」のような短い要求からVEPを進めるとき
- existing transcriptやvalid artifactを再利用し、次のpublic operationを選ぶとき

## Do not use when

- repository実装、Skill authoring、独立監査そのものを依頼されたとき
- workspace、source authorization、利用権限が不明で確認不能なとき

## Inputs

- repository rootとworkspace path
- user request、source、assignment、purpose、audience、length
- explicit authorization／network policy（存在する場合）
- `vep job guide WORKSPACE --json`のread-only guidance

## Output

- reused artifactとcurrent/effective state
- 実行したpublic operationsと各stageのfrozen request／Skill／response schema
- missing authorization、blocked reason、human action
- `HUMAN_REVIEW_READY`または正確なstop stateでのhandoff

## Procedure

1. repository SoTとSkill registryを確認し、production dataをrepository fixtureへコピーしない。
2. workspace内のexisting jobを発見し、`vep job guide WORKSPACE --json`でstored/effective stateとnext operationを読む。guideはprepareを兼ねない。
3. validなacquisition、normalization、transcript、refinement、evidence、draft、audit artifactを再利用する。再生成はintegrityまたはcontract上必要な場合だけ行う。
4. missing authorization、network policy、external API可能性、model provisioning、write side effectを表示し、不足を推測しない。
5. guideが示す1つのpublic operationだけを選ぶ。manifestやtransitionを手編集しない。
6. completed transcriptが有効ならacquisition／normalization／ASRを繰り返さない。transcript-boundary recoveryはeligibleなpublic recovery operationだけを使う。
7. semantic `prepare`が返すfrozen request、stage-specific Skill、response schemaをAIへ渡し、responseをdeterministic `import`へ戻す。
8. refinementは`refine-video-transcript`、evidenceは`analyze-video-evidence`、authoringはstrict task mapping、auditはfresh contextの`independent-content-audit`、revisionは`revise-from-audit`へ委ねる。各Skillのsemantic rulesをこのSkillで再実装しない。
9. audit contextをauthor／revisionへ再利用せず、P0/P1があれば許可されたbounded revisionを行う。解消不能ならstopして人間へ報告する。
10. `HUMAN_REVIEW_READY`で停止し、human approvalが必要と示す。AIがapprove、export、Ready化を代行しない。

## State discipline

- `EXPORTED`: completeとして追加操作しない。
- `HUMAN_REVIEW_READY`: human approval待ちとして停止する。
- `BLOCKED`: guideが具体的なrecovery eligibilityを示す場合だけ案内し、repairを捏造しない。
- integrity error: public validation結果を報告し、manifest修正やartifact差替えを提案しない。

## Validation

- next operationがguide、effective state、authorizationと一致する
- valid artifact reuseにより不要なacquisition／normalization／ASRを行っていない
- semantic responseがfrozen stage Skillとdeterministic importを経由する
- authoringとauditのcontextが独立し、AIがhuman approvalをしていない
- manifest、transition、canonical artifactへのdirect editがない

## Prohibited behavior

- manifest、transition、approval recordのmanual edit
- authorization、network permission、owner、human approvalの推定
- unnecessary reacquisition、normalization、ASR、valid artifactの無理由再生成
- canonical workspaceへのsemantic agent直接書込み
- semantic content rulesのコピーまたはstage-specific Skillの省略
- audit context reuse、AIによるhuman approval、勝手なexport
