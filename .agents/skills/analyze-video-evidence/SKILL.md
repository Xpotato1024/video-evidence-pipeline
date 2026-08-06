---
name: analyze-video-evidence
description: 検証済みtranscript・frame・assignmentから、本文を書かずに証拠台帳と曖昧点を構築する。
---
# analyze-video-evidence


## Status guard

このSkillはdraftであり、evidence Schemaとinspection CLIが実装されるまで実運用しない。

## Inputs

- validated assignment requirements
- transcript segments
- frame index／observations
- user notes

## Outputs

- evidence records
- topic map
- ambiguity list
- human replay requests

## Procedure

1. 発言、画面観察、推論、ユーザー意見を分類する。
2. 各evidenceをtimestampとsource IDへ結び付ける。
3. 矛盾・聞取り不明・視覚情報不足を明示する。
4. レポート本文は生成しない。

## Stop conditions

- source lineageがない
- timestampがduration外
- assignment requirementsが未検証

## Prohibited behavior

- 文字起こしを原本として上書きする
- 根拠のない補完を行う

