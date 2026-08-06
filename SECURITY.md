# Security policy

## Report privately

認証情報、Cookie、署名付きURL、配布限定動画、個人情報、外部送信に関する脆弱性を通常の
Issueへ投稿しないでください。GitHubのprivate vulnerability reportingが有効な場合はそれを
使用し、利用できない場合はrepository ownerへ非公開経路で連絡してください。

## Scope

- acquisition時の認証情報漏えい
- signed CDN URLの永続化
- path traversal／任意ファイル上書き
- external AI APIへの無許可送信
- raw sourceの改変
- approval hash bypass
- CI artifactへの実データ混入

## Handling

秘密情報らしき値を発見した場合、値を再掲せず処理を停止します。コミット済みの場合は、
削除コミットだけで済ませず、credential rotationと履歴上の露出範囲確認を行います。
