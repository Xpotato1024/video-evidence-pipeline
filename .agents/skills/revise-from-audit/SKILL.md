---
name: revise-from-audit
description: 監査所見と証拠に限定してdraftを修正し、resolution ledgerを生成する。
---
# revise-from-audit


## Status guard

このSkillはdraftであり、claim／finding Schema実装前は実運用しない。

## Inputs

- target draft
- structured findings
- validated evidence

## Outputs

- revised draft
- revised claims
- resolution ledger
- unresolved findings

## Procedure

1. P0、P1、P2の順で所見を確認する。
2. 各変更をfinding IDとevidence IDへ対応させる。
3. 監査範囲外の全面改稿を避ける。
4. 修正不能な所見は人間へエスカレーションする。

## Prohibited behavior

- 根拠不足を新しい事実で埋める
- findingを黙って無視する

