---
name: draft-video-report
description: 検証済み証拠と課題要件から、設問に直接答える論証型レポート初稿を作る。
---
# draft-video-report

## Execution boundary

このSkillはfrozen authoring requestに対するsemantic responseだけを生成する。request検証、文字数判定、
workspace書込み、artifact公開、state transitionはdeterministic executorだけが行う。外部API利用はjobの
authorizationが明示的に許可する場合に限る。

## Core principle

reportはsource全体のgeneric summaryではない。優先順は`assignment question → direct answer / central claim →
evidence → rationale → representative detail`である。

## Inputs

- assignment question／requirements、purpose、audience
- validated evidence bundle
- user-authored notes／opinions
- deterministic length、format、language、style constraints
- response schema

## Outputs

- provider-neutral draft Markdown
- evidence／requirementへ束縛したclaim records
- ordered requirement map。omission rationaleは
  `requirement_map.entries[].explanation`へ記録する
- unresolved itemとconfirmation requirement。専用fieldを増やさず、該当entryを
  `partial`／`unmet`として説明し、user opinionは`[要確認]`と
  `requires_user_confirmation = true`でも表す

## Procedure

1. assignment question、purpose、audienceから、直接答えるべき問いと評価条件を特定する。
2. direct answerまたはcentral claimを先に決める。証拠不足なら断定せずunresolvedにする。
3. central claimを支える最小十分なevidenceとrationaleを選び、無関係なevidence abundanceをomitする。
4. 設問の理解や説得力に役立つsource-backed number、name、criterion、representative exampleを必要な分だけ選ぶ。
5. 各material claimをexact evidence IDとrequirement IDへ束縛する。
6. 動画内容、合理的推論、外部事実、user-authored opinionを区別する。
7. user opinionが必須で未提供なら、その箇所をliteral `[要確認]`とconfirmation requirementで表す。
8. short reportではtopic coverageより問いへの答えと最も強い根拠を優先する。
9. redundancy、example列挙、source順の紹介を除き、deterministic length constraintへ適合させる。

## Stop conditions

- user opinionが必須だが提供されず、`[要確認]`を許容しないcontract
- 必須requirementへ答えるevidenceがなく、unresolved outputも許容されない
- frozen request／Skill identityまたはresponse schemaが検証できない

## Validation

- 冒頭または明確な位置でassignment questionへ直接答える
- evidence selectionがcentral claimを支え、無関係なabundanceや同種exampleを抑える
- material claim、rationale、concrete detailがexact evidence／requirementへ束縛される
- source内容とuser opinionが区別され、未提供の立場を作らない
- reportが要約のtopic inventoryへ退化せずlength constraintに適合する

## Prohibited behavior

- ユーザーの感想、立場、経験を捏造する
- evidenceまたはrequirementにないmaterial claimを作る
- 設問を時系列要約やsource全体の均等紹介へ置換する
- representative exampleをgeneral ruleへ昇格する
- 数字や固有名詞を具体的に見せるためだけに追加する
- workspace、Git、GitHub、外部serviceへ直接書込む
