---
name: independent-content-audit
description: fresh contextでdraftのfaithfulnessとselection qualityを独立監査し、P0/P1/P2 findingを作る。
---
# independent-content-audit

## Execution boundary

このSkillはfrozen audit requestに対するfresh-context semantic responseだけを生成する。request検証、差分生成、
workspace書込み、artifact公開、state transitionはdeterministic executorだけが行う。外部API利用はjobの
authorizationが明示的に許可する場合に限る。

## Independence requirements

- authorと別のexecution contextを使う
- author private reasoning、prompt history、self-evaluationを入力しない
- target draftからclaimを独自に再抽出し、source evidenceへ直接アクセスする
- revision時に過去auditor contextを再利用しない

## Inputs

- fixed audit bundle、target draft
- assignment requirements、purpose、audience、task type、length constraint
- validated evidence（roles、source refs、ambiguityを含む）

## Outputs

- independently extracted claims
- factual、omission、hierarchy、specificityを区別したstructured findings
- P0/P1/P2 audit report

## Audit dimensions

`all claims supported != good summary`。faithfulnessとselection／salienceを別々に監査する。

- Faithfulness: unsupported material claimはP1以上。捏造、source contradiction、exampleの一般則化を検出する。
- Selection: assignment-critical content、central thesis/conclusion、safety、deadline、prohibition、exception、
  decision/actionの欠落と、peripheral detailによるcore dilutionを検出する。
- Specificity: sourceに有用なconcrete anchorがあるのにgenericすぎないかを確認する。entity stuffingは要求しない。
- Structure: meeting、operational、technical、reportなどtask-specificな成果物として機能するかを確認する。

## Severity guidance

P1 candidate:

- assignment-critical content omission
- thesis/conclusion omissionによる意味の歪み
- safety-critical omission
- deadline/prohibition/exception omissionによる実務上の誤解
- representative exampleのgeneral rule化
- decision/action item omissionによりmeeting recordとして機能しない

P2 candidate:

- equal-weight topic inventory、peripheral detail過多、redundant examples
- core message dilution、hierarchyの弱さ
- sourceにpurpose上有用なanchorがあるのに過度にgeneric

severityは影響に基づき、語句一致や特定の表現への不一致だけで付けない。

## Procedure

1. independence、bundle hash、frozen Skill identityを検証する。満たせなければ停止する。
2. assignment、purpose、audienceからcritical unitsとtask-specificな成功条件を独立に定める。
3. target draftからmaterial claims、omissions、emphasis hierarchy、specificity choicesを再抽出する。
4. 各claimをevidence、SourceRef、requirementへ直接照合し、unsupported claimを記録する。
5. sourceのcritical unitsとdraft selectionを比較し、faithfulだがflattened／peripheral-onlyなdraftを検出する。
6. concrete anchorsがsourceに存在しpurpose上有用な場合だけ、欠落または詰め込みを評価する。
7. meetingではdecision、action、owner、due、open question、rationaleのsource-backed保持と欠損値の非捏造を確認する。
8. findingをfactual／omission／hierarchy／specificityへ分類し、severity、draft span、evidence、required correctionを示す。
9. supportedでselectiveかつ要件を満たすclean draftへP0/P1を作らず、本文は変更しない。

## Stop conditions

- fresh contextを確保できない
- audit bundle hashまたはrequest identityが変化した
- source evidenceへ直接アクセスできない
- assignment／purposeを判定できずselection qualityを監査できない

## Validation

- factual supportとsalienceを別々に判定している
- assignment-critical omissionとperipheral-only supportを見逃していない
- findingがdraft span、evidence、requirement、severity reasonへ束縛される
- useful specificityとentity/number stuffingを区別している
- clean selective summary／valid meeting minutesをmaterial findingなしで通す

## Prohibited behavior

- author history、claim ledger、自己評価だけを根拠に合格させる
- 同じcontextを独立監査と称する
- 監査と修正を同時に行う
- supportedという理由だけでflattened／peripheral-only draftをcleanにする
- sourceにないanchor、owner、deadlineを要求する
- workspace、Git、GitHub、外部serviceへ直接書込む
