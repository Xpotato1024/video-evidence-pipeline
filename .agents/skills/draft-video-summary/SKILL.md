---
name: draft-video-summary
description: 検証済み証拠から、論証レポートとは別契約の要約とcoverage mapを生成する。
---
# draft-video-summary


## Status guard

このSkillはdraftであり、evidence Schema実装前は実運用しない。

## Inputs

- validated evidence bundle
- summary purpose／length／audience

## Outputs

- summary draft
- topic coverage map
- omitted-topic rationale

## Procedure

1. 目的に応じて中心命題と主要論点を選ぶ。
2. 時系列ではなく情報構造を優先する。
3. 重要な例外、条件、結論を保持する。
4. 考察を要求されない限り新しい評価を加えない。

## Stop conditions

- 要約目的または長さが未定義
- source coverageが不足

## Prohibited behavior

- レポート用のユーザー意見を要約へ混入する

