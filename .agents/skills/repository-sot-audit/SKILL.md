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
2. `.agents/policy.md`の検証規則に従い、対象diffと既存証拠の条件を照合する。環境が利用可能なら再同期しない。必要な環境構築は許可されたnormal host dependencyの範囲に限る。
3. 必要なrepository validatorと関連するfocused tests / lintを選び、実行または有効な証拠で充足する。失敗時は原因を確認して該当stepと影響範囲から再開し、全件をやり直さない。
4. エラーを重大度付きで整理する。
5. 修正依頼がない限り、監査結果だけを返す。

## Stop conditions

- repository rootを特定できない
- lockfileとmanifestが競合する
- 秘密情報らしき値を発見した

## Validation

選択したcommandのexit code、再利用した証拠と条件、未実行理由を報告する。不完全な監査は確認済み範囲を保持して不足だけを補い、未確認をPASSにしない。secret疑いは値を再掲せず、path・重大度・停止した範囲だけを示す。

## Prohibited behavior

- エラーを無視して成功扱いする
- 実メディアを開く、外部送信する

