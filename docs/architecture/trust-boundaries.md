---
schema_version: "1.0"
document_id: VEP-ARCH-TRUST
title: "信頼境界とデータ保護"
document_type: security-architecture
status: draft
canonical: true
canonical_for: trust-boundaries
created_at: 2026-08-06T16:07:00+09:00
updated_at: 2026-08-06T16:07:00+09:00
authors:
  - Xpotato1024
  - OpenAI ChatGPT
owners:
  - Xpotato1024
reviewers: []
source_issue: 1
change_control: pull-request
supersedes: []
superseded_by: null
related_documents: ["VEP-ARCH-CONTEXT", "VEP-ARCH-PIPELINE", "VEP-ARCH-ARTIFACTS", "VEP-ARCH-TECH", "VEP-OPS-NETWORK", "VEP-ADR-0001"]
---
# 信頼境界とデータ保護

## 1. 保護対象

- 配布限定動画、字幕、資料
- ブラウザCookie、session token、API token
- 署名付きCDN URL
- ユーザーの課題文、メモ、成果物
- AI入力bundle
- approval record
- ローカルファイルpathや個人情報

## 2. Trust boundary

### TB-1: External source → local acquisition

脅威:

- 未認可取得
- 期限切れ／偽URL
- signed URLのログ流出
- HTMLを動画として保存
- malicious media

対策:

- authorization gate
- allowlisted adapter
- network acquisition専用permissionとruntime credentialの分離
- TLS verification、manual redirect、public-address DNS検査、接続先IP pin
- credentialのorigin拘束とcross-origin転送禁止
- transport-level content type／error body、size、streaming SHA-256検査
- stable locatorとephemeral download URLの分離
- private stagingからimmutable raw sourceへのexclusive publish

### TB-2: Browser credential → acquisition tool

脅威:

- 全サイトCookieのexport
- CookieファイルのGit混入
- subprocess logへの露出

対策:

- `--cookies-from-browser`等の一時利用を優先
- persistent cookie exportを標準にしない
- credential pathをGit管理外
- process output redaction
- 最小権限の公式APIを優先
- yt-dlpのuser config、plugin、remote component、任意引数、sidecar出力を禁止

### TB-3: Local data → optional cloud AI

脅威:

- 利用条件に反する外部送信
- 過剰な動画／個人情報送信
- provider retention
- model outputへの機密反映

対策:

- `allow_external_api: false`を既定
- job単位の明示opt-in
- 必要最小限のclip／textだけ送信
- providerとmodelをmanifestへ記録
- raw media全体送信を別権限にする
- local backendを標準経路にする

### TB-4: Local workspace → GitHub

脅威:

- 実メディア、字幕、成果物のcommit
- `.env`、Cookie、signed URLのcommit
- CI artifactへの漏えい

対策:

- `.gitignore`
- repository validator
- pre-commit scan
- file size／extension gate
- secret pattern scan
- synthetic fixtureのみCIへ投入
- actual job directoryは`.local/`

### TB-4a: Local workspace → same-host user／process

脅威:

- restricted source、assignment、absolute local path、authorization recordの他ユーザーからの読取り
- permissive umaskによるruntime fileの意図しないgroup／world公開
- temporary file、lock file、final target間のpermission不一致

対策:

- 新規POSIX workspaceのdirectoryを作成時に`0700`へ設定・検証
- 新規POSIX runtime file、raw artifact、temporary／lock fileを`0600`へ設定・検証
- private mode適用失敗をsilent successにせず、restricted source処理前にfail closed
- atomic replace／exclusive hard-link後もfinal targetのprivate modeを維持
- workspace escapeとsymbolic link／junction拒否をpermission設定と独立して維持
- WindowsではPOSIX modeを保証とせず、同一ユーザーのOS account／NTFS ACL境界へ依存
- Windows ACLの自動再構成は本MVPの非スコープとして明示

### TB-5: Author agent → auditor agent

脅威:

- 同一誤読の継承
- author claim ledgerの追認
- prompt historyによる独立性喪失

対策:

- fresh context
- audit bundleの固定
- 本文からclaim再抽出
- source evidence直接参照
- author thought processを入力しない
- structured findings

### TB-6: AI output → human approval

脅威:

- AIが自動承認
- 承認後draftのすり替え
- P0/P1残存
- 自分の意見の捏造

対策:

- approval hash
- P0/P1 gate
- user opinion marker
- human-review checklist
- approval後の変更でapproval invalidation

## 3. Data classification

| Class | 例 | Git | External AI |
|---|---|---:|---:|
| Public definition | code, docs, Schema | 可 | 可 |
| Synthetic test | artificial video/captions | 可 | 可 |
| Restricted source | lecture video, captions | 不可 | 明示許可時のみ |
| User confidential | assignment, notes, draft | 原則不可 | 明示許可時のみ |
| Secret | Cookie, token, signed URL | 不可 | 不可 |

## 4. Logging

ログへ出してよい情報:

- adapter name
- redacted host
- status
- byte size
- duration
- artifact ID
- content hash
- error category

ログへ出してはいけない情報:

- full signed URL
- stable URLを含むfull URLとquery
- redirect Location、Panopto download URL、Box CDN URL
- Cookie/header
- API token
- credential environment value／local credential path
- yt-dlp raw stdout／stderr
- transcript全文
- user draft全文
- local secret pathの内容

## 5. 削除と保持

初期実装では自動保持期間を固定しません。jobごとに保持方針を持たせ、削除は次を区別します。

- source deletion
- derived artifact deletion
- index deletion
- approval/export retention
- logs

削除後もGitHub、cloud provider、backupに複製がないことを保証できない場合、その限界を明示します。

## 6. 非目標

本システムはDRM回避、アクセス制御突破、他者アカウントの利用、禁止されたコンテンツ取得を目的としません。
