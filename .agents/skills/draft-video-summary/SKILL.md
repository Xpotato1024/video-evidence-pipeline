---
name: draft-video-summary
description: 検証済み証拠から、論証レポートとは別契約の要約とcoverage mapを生成する。
---
# draft-video-summary


## Execution boundary

このSkillは明示選択されたsemantic responseだけを生成するinstruction-only contractである。
request検証、workspace書込み、artifact公開、state transitionはdeterministic executorだけが行う。
外部API利用はjob authorizationが明示的に許可する場合に限る。

## Inputs

- validated evidence bundle
- assignment requirements
- summary purpose／length／audience and output constraints

## Outputs

- provider-neutral summary draft
- evidenceへ束縛したclaim records
- requirement／topic coverage map
- omitted-topic rationale and unresolved item list

## Procedure

1. 目的に応じて中心命題と主要論点を選ぶ。
2. 時系列ではなく情報構造を優先する。
3. 各claimを許可されたevidence IDとrequirement IDへ対応させる。
4. 重要な例外、条件、結論を保持する。
5. 考察を要求されない限り新しい評価を加えず、ユーザー意見を生成しない。

## Stop conditions

- 要約目的または長さが未定義
- source coverageが不足

## Prohibited behavior

- レポート用のユーザー意見を要約へ混入する
- evidenceにない評価、事実、timestampを追加する
- workspace、Git、GitHub、外部serviceへ書込む

