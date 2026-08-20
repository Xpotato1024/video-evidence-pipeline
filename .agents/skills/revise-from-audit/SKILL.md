---
name: revise-from-audit
description: 監査のfactual・omission・hierarchy・specificity findingだけを証拠に基づいて限定修正する。
---
# revise-from-audit

## Execution boundary

このSkillはfrozen revision requestに対するsemantic responseだけを生成する。request検証、deterministic diff、
workspace書込み、artifact公開、state transitionはdeterministic executorだけが行う。外部API利用はjobの
authorizationが明示的に許可する場合に限る。

## Inputs

- target draft、structured audit findings
- validated evidence、assignment、purpose、audience、length constraint
- accepted finding IDsとresponse schema

## Outputs

- accepted findingsに限定したrevised draft／claims
- finding ID、draft span、evidence、changeを結ぶresolution ledger
- unresolved／deferred findings

## Procedure

1. frozen requestとSkill identityを確認し、P0、P1、P2の順でaccepted findingsを読む。
2. findingをfactual、omission、hierarchy、specificityに分類し、要求された修正範囲を定める。
3. factual findingはunsupported spanの削除・弱化・source-supported置換だけを行う。
4. omission findingは指定されたcritical unitをexact evidenceから補い、finding外の新topicを追加しない。
5. hierarchy findingは既存supported contentの順序・比重・重複だけを調整する。
6. specificity findingはsourceにある有用なanchorを必要な分だけ加えるか、irrelevant stuffingを除く。
7. 各変更をfinding ID、exact evidence ID、対象draft spanへ対応させる。
8. deterministic length constraintに合わせる必要がある場合も、finding解決に必要な最小変更へ限定する。
9. 修正不能なfindingを黙って消さず、理由とともにunresolvedとして人間へ戻す。

## Stop conditions

- accepted finding、target span、required evidenceのいずれかが欠ける
- finding解決にevidence外の事実または全面rewriteが必要
- frozen request／Skill identityが一致しない

## Validation

- すべてのchange hunkがaccepted findingとexact evidenceへ束縛される
- omitted critical unit、hierarchy、specificityの修正がfindingを直接解決する
- finding外のsupported content、voice、structureを不必要に変更しない
- 新しいunsupported claim、owner、deadline、number、nameを追加しない
- unresolved findingと理由がresolution ledgerに残る

## Prohibited behavior

- finding外の全面rewriteやstylistic polish
- 根拠不足を新しい事実で埋める
- sourceにないconcrete anchor、owner、deadlineを追加する
- findingを黙って無視またはresolved扱いする
- audit findingを新しいselection policyへ勝手に拡張する
- workspace、Git、GitHub、外部serviceへ直接書込む
