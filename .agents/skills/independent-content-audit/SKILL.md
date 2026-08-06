---
name: independent-content-audit
description: fresh contextで成果物の主張を再抽出し、証拠・要件との整合性をP0/P1/P2で監査する。
---
# independent-content-audit


## Status guard

このSkillはdraftであり、audit bundle生成とSchema実装前は実運用しない。

## Independence requirements

- authorと別のexecution context
- author private reasoningを入力しない
- source evidenceへ直接アクセス

## Inputs

- fixed audit bundle
- target draft
- assignment requirements

## Outputs

- independently extracted claims
- structured findings
- audit report

## Procedure

1. 本文から主張を独自に抽出する。
2. evidence、timestamp、requirementへ照合する。
3. author claim ledgerと最後に差分比較する。
4. P0／P1／P2へ分類し、本文は変更しない。

## Stop conditions

- fresh contextを確保できない
- audit bundle hashが変化した
- source evidenceを確認できない

## Prohibited behavior

- authorの自己評価を根拠に合格させる
- 監査と修正を同時に行う

