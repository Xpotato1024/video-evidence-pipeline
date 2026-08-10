---
name: draft-video-report
description: 検証済み証拠と課題要件から、論証型レポート初稿とclaim ledgerを生成する。
---
# draft-video-report


## Execution boundary

このSkillは明示選択されたsemantic responseだけを生成するinstruction-only contractである。
request検証、workspace書込み、artifact公開、state transitionはdeterministic executorだけが行う。
外部API利用はjob authorizationが明示的に許可する場合に限る。

## Inputs

- validated assignment requirements
- validated evidence bundle
- user-authored notes／opinions
- length and style constraints

## Outputs

- provider-neutral draft Markdown
- evidence／requirementへ束縛したclaim records
- ordered requirement map
- unresolved item list

## Procedure

1. 設問への結論と論証構造を先に定める。
2. 各主要主張を許可されたevidence IDとrequirement IDへ対応させる。
3. 動画要約、合理的推論、外部事実、ユーザー考察を区別する。
4. ユーザー意見を生成せず、必要箇所はliteral `[要確認]`とconfirmation requirementで表す。
5. 根拠不足の必須論点は未解決として残し、断定で補完しない。

## Stop conditions

- ユーザー意見が必要だが提供されていない
- 必須要件を満たす証拠がない

## Prohibited behavior

- ユーザーの感想・立場を捏造する
- evidenceまたはrequirementにないmaterial claimを作る
- 設問を時系列要約へ置換する
- workspace、Git、GitHub、外部serviceへ書込む

