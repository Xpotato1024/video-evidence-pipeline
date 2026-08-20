---
name: refine-video-transcript
description: raw transcriptの意味と曖昧性を保ち、supporting evidenceで裏付けられた範囲だけを補正する。
---
# refine-video-transcript

## Execution boundary

このSkillはfrozen refinement requestに対するsemantic responseだけを生成する。request検証、workspace書込み、
artifact公開、state transitionはdeterministic executorだけが行う。raw transcriptはimmutableであり、
Skillの選択は新しい取得・外部送信・書込み権限を与えない。

## Inputs

- segment IDとtimestampを持つraw transcript
- requestに明示されたnon-transcript supporting evidence
- mechanical cleanup済みtextとallowed operation／response schema
- assignment、purpose、audience（補正範囲を変えず、曖昧性の提示にのみ使用）

## Outputs

- raw segmentへ1対1で対応するrefined segments
- correctionごとのbefore／after、reason、supporting evidence refs
- preserved ambiguityとunresolved items

## Correction boundary

句読点、空白、明白なfiller整理など意味を変えないmechanical cleanupと、語、number、proper noun、technical termを
変えるsemantic correctionを区別する。semantic correctionにはtranscript自身とは独立したsupporting evidenceが
必要である。context上もっともらしいだけでは根拠にならない。

## Procedure

1. raw、mechanical cleanup、supporting evidence、frozen Skill identityを確認する。
2. segment boundaries、timestamp、発言順、atomic propositionsを固定する。
3. meaning-preservingなmechanical cleanupとsemantic correction candidatesを分ける。
4. semantic candidateごとに、non-transcript evidenceがexact replacementを直接支持するか確認する。
5. 支持されるcandidateだけを最小spanで補正し、before／afterとevidence refsを記録する。
6. proper noun、number、technical term、否定、主体が未確定なら原文またはambiguityを保つ。
7. propositionsをmerge、split、reorderせず、sourceが持つhesitationやuncertaintyを意味が変わる形で消さない。
8. すべてのsemantic correctionを再検証し、unsupported candidateを出力から除く。

## Stop conditions

- raw segment identity／timestampが欠ける
- supporting evidence identityまたはfrozen requestが検証できない
- schemaを満たすにはproposition mergeやunsupported correctionが必要

## Validation

- semantic correctionはすべてnon-transcript evidenceへ束縛される
- unsupported proper noun、number、technical termの推測がない
- ambiguity、negation、speaker meaning、atomic proposition境界が保持される
- raw transcriptは変更されず、refined outputが別artifactとして返される

## Prohibited behavior

- raw transcriptやcanonical workspaceを直接上書きする
- transcript contextだけでproper noun、number、technical termを訂正する
- 読みやすさのためにpropositionをmerge、split、reorderする
- ambiguityをもっともらしい語で確定する
- supporting evidenceにない情報を追加する
