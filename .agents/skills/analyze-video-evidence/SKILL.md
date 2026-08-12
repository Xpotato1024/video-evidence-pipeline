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
- inputに存在するPDF page/text、image、user notes

## Outputs

- evidence records
- topic map
- ambiguity list
- human replay requests

## Procedure

1. 発言、画面観察、推論、ユーザー意見を分類する。
2. 1 evidence recordを1つのatomic propositionに限定し、複数sourceを使う場合は全sourceが同じpropositionを支持することを確認する。離れた発言を結合して新しい関係を作らない。
3. 各evidenceをtimestampとsource IDへ結び付ける。
4. 矛盾・聞取り不明・視覚情報不足を明示する。
5. レポート本文は生成しない。

## Stop conditions

- source lineageがない
- timestampがduration外
- assignment requirementsが未検証

## Prohibited behavior

- 文字起こしを原本として上書きする
- 根拠のない補完を行う
- 別々のsourceが述べる事実を、支持されていない複合事実へ統合する
