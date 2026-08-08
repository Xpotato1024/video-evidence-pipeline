---
schema_version: "1.1"
document_id: VEP-OPS-NETWORK
title: "Network acquisition運用"
document_type: operations
status: draft
canonical: true
canonical_for: network-acquisition-operations
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-ARCH-PIPELINE
  - VEP-ARCH-ARTIFACTS
  - VEP-ARCH-TRUST
  - VEP-ARCH-TECH
  - VEP-OPS-CLI-LOCAL
  - VEP-OPS-CI
  - VEP-ADR-0001
---
# Network acquisition運用

## Source locator

jobに保存するlocatorはcredentialを含まないstable identityに限定します。

| source kind | canonical locator | 備考 |
|---|---|---|
| `local_file` | 既存のabsolute local path | 既存contractを維持 |
| `direct_http` | stable HTTPS URL | userinfo、fragment、credential／signed queryを拒否 |
| `generic_ytdlp` | stable HTTPS webpage URL | 通常queryは許可し、secret queryだけ拒否 |
| `panopto` | `panopto://<tenant>/session/<uuid>` | viewer URLは保存前にtenant／session identityへ縮約 |
| `box` | `box://file/<file-id>` | file URLは保存前にfile IDへ縮約 |

locator fingerprintはcanonical locatorから計算します。Panopto download URL、Box CDN URL、redirect
`Location`、signed URLはruntime memory内だけで扱い、job、manifest、transition、log、exceptionへ保存しません。

## Authorizationとcredential

すべてのsourceに`allow_local_processing=true`が必要です。network sourceにはさらに
`allow_network_acquisition=true`が必要で、既定はdenyです。`allow_external_api`はcloud AI等の別権限であり、
network acquisition gateには使用しません。fieldがない旧authorization recordはdenyとして読み込みます。

tokenは`--box-token-env`または`--panopto-token-env`で指定した環境変数からruntimeだけで読みます。
browser cookieは`--cookies-from-browser`が明示された`generic_ytdlp`、または明示fallbackを有効にした
`panopto`だけで使用します。token value、Cookie file、Authorization header、任意のyt-dlp引数はCLI入力に
できません。adapterに無関係なcredential optionはfail closedです。

## Shared acquisition

network adapterはmetadataだけのprobeを許容し、probeのためにbodyをdownloadしません。取得bodyはprivate
stagingへ一度だけ書き、regular file、non-zero、maximum bytesを検証してstreaming SHA-256を計算します。
検証後に`source/raw/<sha256>/<sanitized-name>`へexclusive publishし、source／stage manifestを作成します。
POSIXではdirectory `0700`、file `0600`を維持します。WindowsではPOSIX modeを保証しません。

有効なnetwork bundleが`ACQUIRED`なら、再実行はraw bytes、fingerprint、manifestをローカル検証して再利用し、
networkへ接続しません。source manifest公開後の停止はstage manifestをローカル再構築して復旧します。
再取得が必要な場合は新jobを作成します。`local_file`の再probeとcontent-change detectionは維持します。

source manifestのredacted evidenceにはadapter、transport／backend／version、取得時刻、requested／final host、
redirect count、HTTP status category、content type、declared length、actual size、credential mode、warningsだけを
保存します。full URL、query、credential identity／value、生のyt-dlp stdout／stderrは保存しません。

## direct HTTP

HTTPX clientはTLS verificationを有効にし、`trust_env=False`、connect／read／write／pool timeoutを明示します。
redirectは自動追従せず1 hopずつ検証し、relative URLを解決します。loop、上限超過、HTTPSからHTTPへの
downgrade、signed queryへの遷移を拒否します。

DNSはrequest前と実接続時に検証し、接続先を検証済みIPへ固定します。IPv4／IPv6のloopback、private、
link-local、multicast、unspecified、reserved、localhost相当を拒否し、redirect後にも同じ検査を行います。
stream中にもmaximum bytesを強制し、zero byte、Content-Length不一致、HTML／JSON／XMLの明白なerror bodyを
raw mediaとして成功扱いしません。media validityの最終確認は後続のFFmpeg／ffprobe stageで行います。

## yt-dlp

`generic_ytdlp`とPanopto fallbackは固定argvのrunnerだけを使用します。shell、user config、playlist、plugin、
remote component、JS runtime、cache、update、sidecar、subtitle、thumbnail、metadata embedding、netrcを無効化し、
単一fileでaudio／videoを含むformatに限定します。任意引数、external downloader、postprocessorを受け付けません。
outputはprivate staging内のexactly one regular non-link fileだけを許可します。stdout／stderrはuser outputへ
転送しません。browser cookieは明示時だけ使用し、Cookie file exportは標準経路にしません。

## Panopto

official REST APIを優先し、tenant API originにだけenvironment bearer tokenを送ります。session metadataの
`Id`、`Name`、`Urls.DownloadUrl`をPanopto公式OpenAPI contractとして扱い、blank download URLは利用不可です。
download URLはephemeralとして使用し、bearer tokenを転送しません。official downloadが利用できない場合、
`--panopto-ytdlp-fallback`とbrowser cookieの両方が明示されたときだけyt-dlp fallbackを使用します。
access denial、DRM、extractor failureを回避しません。

## Box

official Files APIでfile IDのmetadataを確認し、content endpointを取得します。bearer tokenはBox API originだけへ
送り、302先のsigned CDNへ転送しません。file-not-readyの`Retry-After`は回数と秒数をboundedに扱います。
Box SHA-1等はSHA-256の代用にせず、実bytesから独自にSHA-256を計算します。shared-link passwordは非対応です。

## Troubleshooting

- `NETWORK_ACQUISITION_NOT_AUTHORIZED`: network permissionを明示して再認可可能な新jobを作成する。
- `NETWORK_SSRF_REJECTED`: public HTTPS destinationか確認する。検査無効化optionはない。
- `NETWORK_TIMEOUT`／`SOURCE_TOO_LARGE`: bounded runtime optionを見直す。TLS無効化はできない。
- `YTDLP_NOT_AVAILABLE`: lock済みdependencyを`uv sync --locked --all-groups`で復元する。
- `PANOPTO_CREDENTIAL_REQUIRED`／`BOX_CREDENTIAL_REQUIRED`: token valueではなく環境変数名を指定する。
- `PANOPTO_DOWNLOAD_UNAVAILABLE`: tenant policyを確認する。DRMやdownload禁止を迂回しない。

## Live test boundary

CIはHTTPX MockTransport、fake resolver、fake subprocess runnerと合成bytesだけを使用します。実Box／Panopto
credential、実講義動画、実CookieをCIへ入れません。manual smoke testを行う場合も明示opt-in、artifact upload
なし、credential出力なしとし、未実行なら`Not Run`と報告します。
