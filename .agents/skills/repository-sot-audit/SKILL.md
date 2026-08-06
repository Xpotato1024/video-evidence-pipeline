---
name: repository-sot-audit
description: 文書SoT、Skill台帳、リンク、禁止ファイル、秘密情報、workflow pinを読み取り専用で監査する。
---
# repository-sot-audit


## Use when

- 文書、`.agents/`、workflow、repository policyを変更したとき
- PRをReady化する前の独立確認

## Do not use when

- ドメイン実装の機能試験だけが目的のとき

## Inputs

- repository working tree
- 対象Issue／PR

## Outputs

- validator／test結果
- エラーと警告の一覧
- 未実行項目

## Procedure

1. 文書台帳と対象canonical文書を読む。
2. `uv sync --locked --all-groups`を実行する。
3. lint、tests、`scripts/validate_repository.py`を実行する。
4. エラーを重大度付きで整理する。
5. 修正依頼がない限り、監査結果だけを返す。

## Stop conditions

- repository rootを特定できない
- lockfileとmanifestが競合する
- 秘密情報らしき値を発見した

## Validation

全コマンドのexit codeと未実行理由を報告する。

## Prohibited behavior

- エラーを無視して成功扱いする
- 実メディアを開く、外部送信する

