---
schema_version: "1.1"
document_id: VEP-ADR-0001
title: "Network acquisition credential and ephemeral locator boundary"
document_type: architecture-decision-record
status: accepted
canonical: true
canonical_for: network-acquisition-credential-ephemeral-locator-boundary
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-ADR-INDEX
  - VEP-ARCH-TRUST
  - VEP-OPS-NETWORK
---
# ADR-0001: Network acquisition credential and ephemeral locator boundary

## Status

accepted

## Context

PanoptoとBoxはstableなsession／file identityから、一時的なsigned download URLへ解決します。yt-dlpは
browser credentialやplugin環境を参照でき、通常のHTTP clientはredirect、proxy、DNS再解決によって認可外の
宛先へ到達し得ます。secretまたはephemeral URLをjobのSoTにすると、期限切れ、漏えい、再現不能を招きます。

## Decision drivers

- credential valueとsigned URLを永続化しない
- raw sourceのimmutable evidence contractを維持する
- SSRF、cross-origin credential forwarding、subprocess injectionをfail closedにする
- offline synthetic testで境界を再現可能にする

## Considered options

### Stable identityとruntime credentialを分離する

jobにはstable canonical locatorだけを保存し、token／browser cookieを明示runtime configとして与えます。
provider APIが返すdownload URLはmemory内だけで使用します。

### Provider download URLまたはcredentialをjobへ保存する

再実行は単純になりますが、secret persistenceと期限切れURLをSoT化するため採用しません。

### yt-dlpへ任意引数を委譲する

対応範囲は広がりますが、plugin、external downloader、postprocessor、filesystem出力の境界を維持できないため
採用しません。

## Decision

stable locator、runtime-only typed credential config、redacted acquisition evidenceを分離します。HTTPはmanual
redirect、public-address DNS validationと接続先pin、origin-bound bearer、bounded timeout／sizeで取得します。
yt-dlpはlock済みruntime dependencyと固定argv、private single-file stagingでだけ実行します。Panoptoはofficial
APIを優先し、tokenを明示tenantへ拘束します。公式APIがdownload禁止を示すblank URLではfallbackせず、tokenなしで
明示browser-cookie時だけfallbackします。Box bearerはAPI originからCDNへ転送しません。yt-dlp通信は
HTTPS-only validating local proxyを固定し、各destinationのpublic-address検査とIP pinを強制します。

network acquisitionには`allow_network_acquisition`を追加し、`allow_external_api`と分離します。

## Rationale

stable identityはjob fingerprintと再利用判断を期限付きURLから切り離します。runtime credentialはauthorizationと
secret sourceを混同せず、redacted evidenceは監査可能性を保ちながらcredential identity／valueを残しません。
固定runnerと共通stagingはadapterごとのpublish差異を防ぎます。

## Consequences

### Positive

- signed URL失効後も有効な`ACQUIRED` bundleをnetworkなしで再利用できる
- provider credentialの送信originと永続化禁止をtestできる
- local／network sourceが同じimmutable raw artifact contractへ収束する

### Negative

- arbitrary yt-dlp option、shared-link password、OAuth client flowは利用できない
- Panopto tenant contractが公式OpenAPIと異なる場合は推測せず失敗する
- FFmpegなしで単一file formatがないsourceは取得できない

### Follow-up

- FFmpeg／ffprobeとformat strategy拡張は後続media issueで扱う
- 明示invalidation／reacquisitionは将来の専用contractで扱う

## Supersession

置換するADRはありません。
