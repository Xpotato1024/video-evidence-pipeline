---
name: prepare-human-review
description: 承認対象draft、差分、監査結果、再生確認箇所を人間確認用packageへ整理する。
---
# prepare-human-review


## Status guard

このSkillはdraftであり、approval manifest実装前は実運用しない。

## Inputs

- current and previous draft
- audit results
- requirement map
- unresolved items

## Outputs

- human review checklist
- timestamp replay list
- draft diff
- approval target hash

## Procedure

1. 人間だけが判断すべき箇所を抽出する。
2. 低確度claimと未解決P2を明示する。
3. 承認対象hashを表示する。
4. 承認そのものは記録しない。

## Prohibited behavior

- AIが`approved`を生成する
- 未解決P0/P1を隠す

