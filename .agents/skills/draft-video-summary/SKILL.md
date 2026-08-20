---
name: draft-video-summary
description: 検証済み証拠から、assignment依存で重要事項を選択して要約または議事録を作る。
---
# draft-video-summary

## Execution boundary

このSkillはfrozen authoring requestに対するsemantic responseだけを生成する。request検証、文字数判定、
workspace書込み、artifact公開、state transitionはdeterministic executorだけが行う。外部API利用はjobの
authorizationが明示的に許可する場合に限る。

## Core principle

要約は`selection + compression`であり、全topicの均等圧縮ではない。faithfulnessを満たすだけでは不十分で、
assignment、purpose、audienceに対して重要なsemantic unitを選ばなければならない。

## Inputs

- frozen assignment requirements、purpose、audience、task type
- validated evidence bundle（source refs、information roles、ambiguityを含む）
- deterministic length、format、language、style constraints
- response schema

## Outputs

- provider-neutral summaryまたはmeeting-minutes draft
- evidenceへ束縛したclaim records
- requirement／selected-unit coverage map
- omitted-unit rationale、unresolved item list

## Salience profiles

task-specificな要求を常に最優先する。そのうえで次を候補として使う。

- lecture／narrative: thesis、conclusion、central rationale、意味を変えるcondition／exception、explicit emphasis
- operational: action、deadline、prohibition、safety、condition／exception、consequence
- meeting: decision、action item、owner、deadline、open question、key rationale
- technical: result、metric、constraint、method choice、risk／trade-off

roleはimportanceそのものではない。同じevidenceでも「全体要約」「具体例中心」「action中心」でselectionを変える。

## Concrete anchors

core pointの識別や理解に役立ち、sourceに存在する場合だけ、1〜数個の高価値anchorを選ぶ。候補はexact number、
percentage、threshold、date/time/deadline、person/organization、system/product/method name、exact option、owner、
acceptance criterion、representative exampleである。数字や固有名詞であることだけを採用理由にせず、短い要約へ
大量に詰め込まない。

## Procedure

1. assignment requirementsを読み、task-specificな問いと必須項目を特定する。
2. purposeを読み、成果物が何の判断・理解・行動に使われるかを確認する。
3. audienceを読み、必要な前提、用語、具体性を決める。
4. evidenceのcontent、roles、source strength、ambiguityを読む。roleを固定scoreへ変換しない。
5. purposeを成立させるcore semantic unitsを選ぶ。
6. coreの理解、根拠、実行に必要なsupporting unitsだけを選ぶ。
7. peripheral、repetitive、housekeeping、assignment非関連unitを明示的にomitする。
8. coreを識別・理解させるsource-backed concrete anchorsを必要に応じて1〜数個選ぶ。
9. 結論先行、論理関係、箇条書き、meeting sectionsなど目的に適したstructureを決める。
10. 各material claimをevidence IDへ束縛し、ambiguityやunknownを保って文章化する。
11. 重複、同種exampleの列挙、topic inventory、coreを薄めるdetailを除く。
12. deterministic length constraintに収まる候補を返す。機械的な文字数検証はexecutorへ委ねる。

## Meeting/minutes profile

meeting assignmentでは、sourceにある範囲で`Decisions`、`Action items`、`Owner`、`Due date`、
`Open questions`、`Key rationale`を保つ。exact number/date/optionやrepresentative exampleは決定や行動の理解に
役立つ場合だけ残す。ownerやdeadlineがsourceにない場合は`unspecified`、未解決なら`unresolved`とし、補完しない。
単なる「予算、日程、機能について話し合った」のようなtopic inventoryで終了しない。

## Stop conditions

- purposeまたはlength constraintが未定義で、requestから一意に回復できない
- assignment-criticalな要求に対応するevidenceがなく、unresolvedとしてもcontractを満たせない
- source-bound claimを作れない、またはfrozen Skill identityが一致しない

## Validation

- central messageまたはassignment-critical unitがpreserveされる
- omitted unitsはpurposeに照らして妥当で、peripheral coverageがcoreを圧迫しない
- material claimsとconcrete anchorsがevidence-boundでunsupported claimがない
- repeated detailがなく、structureとlengthがpurposeに適合する
- meetingではsourceにあるdecision/action/owner/due/open questionが機能的に保持され、欠損値を捏造しない

## Prohibited behavior

- transcript順の逐次圧縮、各topicへの均等文字数配分
- evidence count比例、duration比例、role名だけによるselection
- 「A、B、Cについて説明した」だけのtopic inventory
- repeated examplesの列挙、housekeeping/detailによるcoreの希釈
- 数字、固有名詞、entityの無差別な詰め込み
- evidenceにない評価、事実、timestamp、owner、deadline、user opinionの追加
- workspace、Git、GitHub、外部serviceへの直接書込み
