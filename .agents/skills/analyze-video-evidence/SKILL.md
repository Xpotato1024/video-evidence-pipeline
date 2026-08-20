---
name: analyze-video-evidence
description: 検証済みtranscript・frame・assignmentから、本文を書かずにatomicかつsource-boundな証拠台帳を作る。
---
# analyze-video-evidence

## Execution boundary

このSkillはfrozen evidence requestに対するsemantic responseだけを生成する。request生成・検証、
workspace書込み、artifact公開、state transitionはdeterministic executorだけが行う。Skillの選択は
新しい取得・外部送信・書込み権限を与えない。

## Use when

- executorが検証済み入力とfrozen Skill snapshotを持つevidence requestを用意したとき
- draftを書く前に、再利用可能な証拠台帳、topic、曖昧点を抽出するとき

## Do not use when

- source lineage、assignment、request identityの検証が済んでいないとき
- transcriptの修正、draft作成、監査を求められているとき

## Inputs

- assignment requirements、purpose、audience
- source IDとtimestampを持つ検証済みtranscript segments
- requestに含まれるframe observations、PDF text/page、image、user notes
- evidence response schemaと許可されたinterpretation level

## Outputs

- 1 record 1 atomic propositionのevidence records
- 各recordのsource refs、interpretation level、flags、ambiguity
- topic map、unresolved ambiguity、必要なhuman replay request
- draft本文を含まないschema-compliant response

## Information roles

`role:*`はsourceから妥当に判断できる情報機能であり、importance scoreではない。assignmentによって
同じrecordの重要度は変わる。許可された語彙から必要なroleだけを付け、推測したroleは付けない。

- `role:thesis`, `role:conclusion`, `role:rationale`
- `role:decision`, `role:instruction`, `role:action-item`
- `role:deadline`, `role:prohibition`, `role:condition`, `role:exception`, `role:safety`
- `role:example`, `role:context`, `role:emphasis`, `role:open-question`

`role:example`を低重要度とみなしたり、`role:decision`を常に高重要度とみなしたりしない。

## Procedure

1. assignment、purpose、audienceを読み、抽出対象と必要なsource granularityを確認する。
2. 発言、画面上の直接観察、合理的推論、外部事実、user-authored opinionを区別する。
3. 各recordをsourceが直接または許可されたinterpretation levelで支持する最小のatomic propositionへ限定する。
4. 複数sourceを束ねる場合は、全sourceが同じpropositionを支持することを確認する。離れた発言から新しい関係、因果、主体、期限を作らない。
5. 各recordを正確なsource IDとtimestamp/page/frameへ結び付ける。
6. sourceから明示または妥当に分類できる情報roleだけを付ける。owner、deadline、decision、actionを話題名だけから推測しない。
7. 例と一般則、contextとthesis、提案とdecision、予定と確定事項を区別する。
8. 数字、固有名詞、technical term、否定、条件、例外の不確実性を保持し、矛盾や聞取り不明をambiguityへ出す。
9. schemaとatomicityを再確認し、本文、selection、importance rankingを生成せずに返す。

## Stop conditions

- source lineageがない、またはtimestamp/page/frameが入力範囲外
- assignment requirementsまたはresponse schemaが未検証
- propositionやroleがsourceから判定できず、ambiguityとしても表せない
- frozen requestと実行対象Skill identityが一致しない

## Validation

- すべてのmaterial fieldがexact sourceへ束縛される
- 1 recordに独立して真偽を評価できるpropositionが1つだけある
- role flagsはknown vocabulary内、sorted、uniqueで、importanceを表していない
- ambiguity、contradiction、example/general-rule境界が失われていない
- evidence responseにdraft本文やunsupported synthesisがない

## Prohibited behavior

- transcriptやcanonical workspaceを直接変更する
- proper noun、number、owner、due date、decision、因果を推測で補う
- 別々のsourceをunsupportedな複合事実へ統合する
- evidence件数、duration、roleをimportance scoreへ変換する
- representative exampleをgeneral ruleへ昇格する
- evidence responseの中でsummary/report/minutesを作る
