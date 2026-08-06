---
name: draft-video-report
description: 検証済み証拠と課題要件から、論証型レポート初稿とclaim ledgerを生成する。
---
# draft-video-report


## Status guard

このSkillはdraftであり、evidence／claim Schema実装前は実運用しない。

## Inputs

- validated assignment requirements
- validated evidence bundle
- user-authored notes／opinions
- length and style constraints

## Outputs

- `draft.md`
- `claims.jsonl`
- `requirement-map.json`
- unresolved list

## Procedure

1. 設問への結論と論証構造を先に定める。
2. 各主要主張をevidence IDへ対応させる。
3. 動画要約とユーザー考察を区別する。
4. 根拠不足は`要確認`として停止対象にする。

## Stop conditions

- ユーザー意見が必要だが提供されていない
- 必須要件を満たす証拠がない

## Prohibited behavior

- ユーザーの感想・立場を捏造する
- 設問を時系列要約へ置換する

