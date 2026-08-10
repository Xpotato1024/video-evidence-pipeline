---
name: independent-content-audit
description: fresh contextで成果物の主張を再抽出し、証拠・要件との整合性をP0/P1/P2で監査する。
---
# independent-content-audit


## Execution boundary

このSkillは明示選択されたfresh-context semantic responseだけを生成するinstruction-only contractである。
request検証、author claimとの差分生成、workspace書込み、artifact公開、state transitionはdeterministic
executorだけが行う。外部API利用はjob authorizationが明示的に許可する場合に限る。

## Independence requirements

- authorと別のexecution context
- author private reasoningを入力しない
- author claim／run／prompt historyを入力しない
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
3. unsupported material claimをP1以上として記録する。
4. supportedで要件を満たすclean draftへP0／P1を作らない。
5. P0／P1／P2へ分類し、本文は変更しない。

## Stop conditions

- fresh contextを確保できない
- audit bundle hashが変化した
- source evidenceを確認できない

## Prohibited behavior

- authorの自己評価を根拠に合格させる
- 監査と修正を同時に行う
- author historyまたはclaim ledgerを独立監査の入力として要求する
- workspace、Git、GitHub、外部serviceへ書込む

