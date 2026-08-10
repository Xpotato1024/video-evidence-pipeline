---
name: revise-from-audit
description: 監査所見と証拠に限定してdraftを修正し、resolution ledgerを生成する。
---
# revise-from-audit


## Execution boundary

このSkillは明示選択されたsemantic responseだけを生成するinstruction-only contractである。
request検証、deterministic diff、workspace書込み、artifact公開、state transitionはdeterministic executorだけが
行う。外部API利用はjob authorizationが明示的に許可する場合に限る。

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
2. 各変更をfinding ID、exact evidence ID、対象draft spanへ対応させる。
3. accepted findingの範囲だけを変更し、監査範囲外の全面改稿を避ける。
4. 未解決または修正不能なfindingを黙って消さず、resolution ledgerへ残して人間へエスカレーションする。

## Prohibited behavior

- 根拠不足を新しい事実で埋める
- findingを黙って無視する
- finding／evidence外へ変更を広げる
- workspace、Git、GitHub、外部serviceへ書込む

