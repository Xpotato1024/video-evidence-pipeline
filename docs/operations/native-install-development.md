# 次期導入基盤の開発・検証記録

これは開発中のCLI縦断実装であり、利用者向けinstallerや配布可能なbinaryではない。
通常の導入は引き続き[既存手順](installation-and-updates.md)を使う。
本作業は main `791d1dc54dd142b6c821cd70bcfa7941a421be6c` を基準にしたローカルbranch上で行う。
公開PR、merge、release、binary配布、本番導入、PATH/registry/権限/security設定変更は実施しない。

2026-10-23の実装範囲は、承認済みarchitecture v0.2のP0、P1とP2の最小開発経路。
設計原本のSHA-256は `46c3ee51352cb86b7e16ba95b818176d5c729ac14480dd775045d1cdad91f291`。
P2全体の署名済み標準user受入や、P3のGUI/online updaterまで完了した意味ではない。

## 判断とPoCの範囲

Rustを小さな共通setup engine/launcherの実装候補として継続し、Pythonの処理本体はprivate runtimeに収める。
Goは有力な代替。全面移植は行わない。FFmpeg/CTranslate2の時間・VRAM/model容量はhost言語だけで減らない。

RustとGoのPoCは同じmanifest、16通常file/合計4MiBの合成入力で、起動、SHA-256検証、
検証付きfile copy、同じPython `-I -S` noop worker起動を比較する。
`extract`という操作名だが圧縮archiveの展開ではない。実動画・音声・授業資料は使用しない。
各9回のfresh processを交互・直列実行し、stdoutとcopy後の全file hashの一致を確認する。
生データ、build command/version/flags、source/lock/binary identityは作業領域のevidenceへ保存する。

最初のMSVC動的CRT版では、Rust/Goのexe単体が386,048/3,119,616 bytes、起動中央値14.53/16.25ms、
4MiB検証33.05/29.33ms、copy47.33/41.77msだった。Rustが全操作で速い結果ではない。
DLL検査でRust候補のVCRUNTIME140依存を確認したため、CRT静的リンク候補も別条件として検証する。
後続の測定条件と最終値は下の検証結果へ記録する。

数値の限界:

- unsignedな開発exe単体のsizeであり、Python/packages/resources/notices/署名を含む配布sizeではない。
- memoryはWindows `GetProcessMemoryInfo` のPoC親process lifetime PeakWorkingSetSizeとPeakPagefileUsage。
  Python子processを含むprocess-tree peakではない。commitとRSSを混同しない。
- 共有hostの負荷・OS/AV cache・power stateは隔離していない。cold測定とは呼ばない。
  既知の同時作業があるrunは記録し、改善根拠の主系列から外す。
- timeout修正後のcollectorはsuspended起動→Windows Job割当→resumeを使用する。
  両候補へ同じ測定用処理を加えるが、旧collectorとの時系列比較はしない。
- 正常fixtureの処理が同じでも、Go encoding/json v1とSerdeのduplicate/case/null等の拒否条件は同じではない。
  不正入力防御、署名/TUF、OS GUI、別OS/CPUを同等に実証した測定ではない。

OS/GUI/保守判断:

| 観点 | 確認できる範囲 / 残る条件 |
| --- | --- |
| Rust | MSVC toolchainでWindows x64 build。Serde/SHA-2とその固定transitive依存をCargo.lockで管理。CRT・署名・OS APIの実物確認が必要 |
| Go | Windows amd64 portable toolchainでbuild。PoCに第三者Go moduleなし。Go runtime/GCを含む。cgoを使うGUIなら別toolchain条件になる |
| GUI共有 | versioned UTF-8 JSONLのplan/apply/status/errorを同engineへ委譲。次段階で開発Win32 GUI・opt-in progress/取消を追加。accessibility全体の受入は残る |
| 保守 | Rustの追加crate/SBOMとOS adapter保守、Goのstrict parser追加、二言語構成のbuild管理を別々に評価。LOCや空mainの速度を保守工数とみなさない |
| 対応OS | 新engineの動作検証はWindows x64のみ。Rust/Goのcompiler target一覧を製品対応一覧にしない。Linux/macOS/ARMと署名実機は後続gate |

## 資源・データ・旧job

`ResourceContext`はprogram resources、`DataContext`はjobs/models/bindingsの場所を明示する。
source modeの`.local/jobs/<id>`と既存CLIを維持し、installed modeは`data/jobs/<id>`を使う。
Skill/schema参照、recovery child、ASR model root、study exportの保護対象をこの境界へ接続した。

installed jobのbindingはjob本体外のstateに保存し、release ID/resources/job pathを固定する。
別releaseからの誤読を拒否し、旧版を残して`run --release <id>`で開く。rollbackは既定版の切替で、
job/承認/raw/specの逆変換ではない。新旧jobのbyteをinstallerは移動・削除しない。
失敗したchildの同一bindingは再試行で再利用するが、他版のbindingは書込前に拒否する。

既存source jobをinstalled modeへ自動登録するmigrationは未実装。
bindingのないjobは元checkoutの利用を案内して停止する。現workerで旧jobを読めたことを互換性の証明にしない。
sourceの既存recovery手順は維持する。ASRのinstalled build/provision/workerは、verified build contextと
任意componentの受入が未完のため、Docker/networkより前に明示拒否する。source ASRは従来どおり。

## 専用Python payload

`scripts/build_private_runtime.py`は検証済みWindows公式runtime archiveと、
`uv export --locked --no-dev --no-emit-project`のhash固定requirementsから専用targetへ入れたcore packagesを受け取る。
開発venvをコピーしない。builderの実行はbuild端末側だけで、利用者側のGit/uv/Pythonを前提にしない構成を検証する。

```text
release/
  runtime/         # 今回は公式Windows embeddable CPython
  packages/        # core依存だけ。dev/ASR/GPU groupを除外
  payload/         # src, .agents, schemas, config, ASR契約source, LICENSE, lock
  release.json     # nativeが検証済みmanifestをstageへ記録
```

`payload/src`を保持するのは、現ASR identityがsource/pyproject/Dockerfile等を実行時にもhashするため。
native launcherはprivate interpreterを`-I -B -S -X utf8`で起動し、bootstrapがverified payload/srcとpackagesだけを追加する。
cwd、user site、PYTHONPATHをimportの正本にしない。app/stateをstudy成果物の出力先にしない。
モデルやjobをprogram payloadへ同梱しない。

ローカル検証用CPythonは3.13.17 Windows x64。
[公式manifest](https://www.python.org/ftp/python/3.13.17/windows-3.13.17.json)の
PythonEmbed amd64 archive SHA-256 `0ba1ee4bfcbdc3a559d27e2e9ce76d04a7839947a23f46375872c755c0d463b8`と照合した。
これは再配布のlicense dossierや署名の代わりではない。

## テスト専用CLIとtransaction

`native/setup`は**development sandbox専用**。通常user領域の自動選定や本番installコマンドは持たない。
空のテストdirectoryへ`init-sandbox`でmarkerと専用app/state/dataを作る。
空でないdirectoryは初期化しない。online更新、FFmpeg/ASR取得、self-update、GC、uninstall/purgeを持たない。

入口:

```text
vep-setup --sandbox-root <absolute-test-root> --protocol=jsonl
vep-setup --sandbox-root <absolute-test-root> run [--release <id>] -- <existing VEP CLI args>
```

JSONLはUTF-8/最大64KiB/1request。`schema=vep.setup.v1`, `id`, `op`を送り、resultにid/seq/status/codeを返す。
操作は`capabilities`, `init-sandbox`, `plan`, `apply`, `status`, `rollback`, `repair`。
runのstdio/exitはworkerへ渡し、setup protocolと混在させない。

planはlocal manifestの外部指定SHA-256、target/inventory、現在generationとsandbox rootをplan IDへbindする。
plan/statusは既存lockを取り、FSを書き換えない。apply/rollbackはplan IDの再照合が必要。
**外部指定hashはpublisherの認証ではない**ため、public releaseの導入には使えない。

applyはwriter lockでinstaller間を直列化し、短いselection lockでactivation/statusを保護する。
copy中にはselection lockを保持しない。size/count/path/case/hashを検査して独立stageへcopyし、
scratch contextのdoctorが成功してからversion directoryを確定する。同じIDの異なる内容は上書きしない。
receipt/context/journalを記録し、generation付きactive pointerをreplace後readbackする。
API失敗後は旧/新/不明を区別し、不明は停止する。prepared journalが残れば次の更新を止め、repairで照合する。
Windowsの電源断耐性や任意filesystemでのatomicityを実証済みとはしない。

repairは一致するold/new状態を照合してjournalを確定する。壊れたpointerを推測で再構成せず、
不明なstateやstageを削除しない。全versionを保持するため、この段階ではGC/lease handoffを省略できる。
将来の削除・self-update追加前には設計v0.2のlease、ACL、handle相対操作と復旧gateが必要。

fault injectionは`--features fault-injection`でbuildしたテストbinaryだけに含む。
`VEP_SANDBOX_INTERRUPT`でcopy/promote/prepared/replace/commitの境界を中断する。通常buildにこの環境変数による中断はない。

## 検証の再現と結果

Rust/Goのbuildは直列・1 build job。buildと測定を重ねない。
テストには`VEP_TEST_TEMP_ROOT`でrepository外の新規専用directoryを指定する。
この端末ではOS sandbox内のstrict path canonicalizeだけが失敗したため、read-only preflightで通常権限との差を確認し、
関連テストを通常権限で実行する。productionのfail-closed検査やsecurity設定を緩めていない。

```text
cargo test --offline --manifest-path native/setup/Cargo.toml --features fault-injection -j 1
python -m pytest tests/test_runtime_context.py tests/test_setup_benchmark.py
# 下記opt-inのみ、build済みbinaryとprivate payloadを使用。pytestはdownload/buildしない。
VEP_NATIVE_SETUP_BIN=<test-build>/vep-setup.exe
VEP_PRIVATE_PAYLOAD=<test-payload>
python -m pytest tests/test_native_setup.py
```

最終PoCはCRT静的リンクRustとGoを同じ修正済みcollectorで各9回測定した。
このrun中に本作業のbuild/testは動かしていない。他タスクの共有host負荷は隔離していない。

| 同等fixture操作 | Rust中央値 ms | Go中央値 ms | Rust親peak RSS MiB | Go親peak RSS MiB |
| --- | ---: | ---: | ---: | ---: |
| capabilities起動 | 37.965 | 40.292 | 4.62 | 6.93 |
| 4MiB SHA-256検証 | 54.502 | 50.736 | 4.70 | 8.34 |
| 検証付きcopy | 66.108 | 61.509 | 4.77 | 8.39 |
| 同じPython noop起動 | 84.196 | 87.120 | 5.08 | 7.81 |

exe単体はRust 481,280 bytes、Go 3,119,616 bytes。Rust候補はPE import検査で
VCRUNTIME依存がなくなったが、標準user VMでの動作を保証しない。
小さなnative共通engine候補としてRustを継続する。処理本体の全面移植やASR性能優位の根拠にはしない。
[全72sampleと条件](../../native/poc/results/windows-x64-20261023/results.json)、
[compiler/flags/source/lock/binary identity](../../native/poc/results/windows-x64-20261023/build-provenance.json)を保存した。
途中の静的CRT測定はnativeテストと競合したため主系列から除外した。
EOF空行を整えた最終sourceで再build/再測定し、それ以前の非競合runもidentityを分けて保持した。
非競合run間にも起動16.6/19.6ms→38.0/40.3msの変動があり、host条件を隔離した性能差とは扱わない。
旧collectorとの改善比較も行わない。

専用CPython 3.13.17とcore依存だけから作った実payloadは2,298files / 52,268,495 bytes。
manifest SHA-256は`23910a3ad6eb4f7eb10ea14377176a6b4056233d59f28761e84ea284b2eb0d81`。
これは非圧縮開発payloadのinventoryであり、署名/全第三者noticesを揃えた配布sizeではない。
同payloadの実native `run -- doctor --json`を5回直列起動した中央値は4,131.611ms。
full inventory/hash検査とPython workerを含む。親peak RSS中央値6.14MiBにはPython子のmemoryを含まない。
共有host・warm cache・測定Job割当の条件はPoCと同じ。製品全体のprocess-tree peakは未測定。
現在の全file再検証は起動UXの残るボトルネックであり、認証・改竄脅威モデルなしに省略しない。

実native/private-runtime縦断4件と、追加のcopy/promote中断1件は成功した。
導入→doctor→job作成→更新→旧版明示実行→rollback、新旧job byte保持、
全5地点の強制中断→repair→再利用、stale plan、破損pointer拒否、hash/path拒否、writer競合、
日本語path、stdin EOF前のJSONL応答、worker errorの非0 exit伝播を確認した。
テストは本番user領域を使わない。電源断やdisk-fullの実証とは区別する。

builderの6件は通常path/Windows短名alias包含、input/output junction、manifest既存衝突、
存在しないdrive/shareに相当するroot探索停止を確認した。
runtime context 9件とcollector timeout 1件も成功し、installed recovery再試行、model/data分離、
ASRの早期拒否、export/state保護、legacy未登録拒否を含む。
先行source関連回帰は71成功/3skip。全pytest成功や本番gate完了とは表現しない。
追加のstudy export/ASR policy/legacy/CLI回帰は122成功/1skip/1失敗。
失敗は長いfixture pathのfile作成であり、同一testは短い専用temp rootで1成功。
path長設定や既存source codeを変更して隠していない。

独立レビューはprimaryの実行結果と区別したcode/証拠確認。
入力link取り込み、manifest上書き、短名alias、root探索、JSONL EOF待ち、selection lock範囲、
directory深さ/件数、recovery orphan binding、ASR境界、export/上書き保護の指摘を修正した。
レビュー原本とJUnitはローカル作業領域`evidence/`に保存する。

## 次段階: 起動費用とGUIの信頼境界

2026-10-23、同じlocal branchで継続。起動検証を省かず、`run-profile`を追加した。
stdoutのdoctor JSONは通常起動と同じで、opt-in時だけnative/workerの時間をstderrへ出す。
`scripts/profile_native_launch.py`は新規scratchへ導入し、plain/profileを交互・直列で各5回起動する。
全検証、同じpayload、同じdoctor出力を確認し、別系列でprivate interpreterのnoopを測る。
memoryはこの分解toolでは測らない。前段のnative親RSSをPythonを含むpeakへ付け替えない。

最初の分離測定はplain/profile中央値3,849.496/3,849.369ms、interpreter noop28.600ms。
内訳中央値はreceipt/manifest6.814ms、全file hash＋path検査1,292.339ms、
inventory走査1,171.559ms、context0.435ms、selection1.187ms、sandbox open2.951ms。
worker wall1,359.134msの**内側**にbootstrap/import974.940ms、context8.588ms、
CLI dispatch/doctor262.837msがある。native worker wallと子の内訳を二重加算しない。
残差にはinterpreter起動・bootstrap main前・終了・process制御が含まれ、spawn単独の実測とは呼ばない。
各中央値の和もtotal中央値と厳密一致するとは限らない。
旧4.13秒との改善率を主張しない。共有host、warm cache、AV、power stateは隔離していない。
rawとartifact identityは作業領域`evidence/native-launch-costs-v1.json`に保持する。

| 責務 | 現在必ず行う検証 | 代替できないもの |
| --- | --- | --- |
| 導入 | 外部指定manifest hash、target/path/size/count、stageへcopyしながら全hash、stage inventory、candidate doctor、receipt/context、journal/generation/readback | hash指定はpublisher認証ではない。成功receiptは後日のfile不変性を保証しない |
| 起動 | active/receipt/manifest/context、全file hashとinventory、private Python isolation、jobの版binding | user-writableなfileのmtime/sizeや以前のdoctor成功だけでは改変を除外できない |
| doctor | runtime/resource整合、必須資源、Skillとpromotion等の意味的な導入診断 | 任意sourceの取得許可、実jobの承認、ASR/model/device readyの証明ではない |
| GUI | 同releaseのengineを起動し、plan/resultを検査して表示し、人間の確認・取消要求を伝える | trust/署名/導入可否やcopy/activation/repairをGUIで再実装しない |

起動hash/inventoryの重複path検査は今後の調査候補だが、handle相対操作・owner/DACL・
hardlink/TOCTOU・供給元認証が未完の現在、cacheや既存receiptへ置換しない。
今回の成果は責務と費用の可視化であり、高速化実績ではない。

### 共通engineの進捗と取消

通常の1request/1result JSONLは維持する。`events:true`を明示したclientだけがprogressとcancelを使う。
capabilitiesの`streaming_events`でhandshakeし、入力line64KiB、追加入力最大16line/合計1MiB。
追加の`{"schema":"vep.setup.v1","id":"<同じID>","op":"cancel"}`で協調取消を要求する。
streamingでのstdin EOFは切断として扱い、通常CLIのEOFを暗黙取消へ変更しない。

copy/hashのchunk間、inventory走査、candidate doctor待機中で取消を確認する。
selection lock下でgenerationを再照合した後、prepared journalより前のCASを最後の取消点にする。
取消が先なら`status=cancelled/code=CANCELLED/exit3`。commitが先なら取消を延期して
replace/readback/journal確定まで終え、実際の最終resultを返す。cancel_ackは結果の確定ではない。
stage・旧版・dataを取消によって削除しない。再試行は新しいplanで行う。

protocol出力は専用writerと32frame有界queueへ分離し、progress/ackを間引ける。
critical区間はstdoutやGUIの読取りを待たない。finalは捨てず、全transaction lock解放後に送信を待つ。
consumer停滞はprocess終了を遅らせ得るが、commitや他のstatus/launchのlockを保持しない。
候補doctorはWindowsでsuspended spawn→Job割当→resumeし、取消/timeoutで所有する子孫を終了する。
稼働中の旧版jobを終了する機能ではない。非Windowsの同等process-tree保証は未検証。

### 開発用の薄いWin32 GUI

`native/setup`の`vep-setup-gui.exe`は隣の`vep-setup.exe`をshellなしで起動する。
標準STATIC/EDIT/BUTTON controlsでroot/manifest/payload/hashを入力し、共通planのroot、release、
manifest hash、generation、容量、plan_id、開発用・publisher未認証をそのまま確認する。
checkboxで確認してからapply/rollbackし、入力変更時はplanを無効化する。
初期化とrepairも対象を確認して共通engineへ委譲する。GUIにfile copy/activation/recovery実装はない。

GUIのline上限64KiB、event channel16frame。ID/連番/final/exit codeを検査し、
異常EOFや不正protocolではstdout/stdinを閉じてから終了を待ち、別status要求でstateを照会する。
不明な結果を成功・取消へ推測しない。pending取消はstdin準備前から保存し、準備直後に送る。
実行中の画面Closeも取消要求として扱い、最終結果を待って閉じる。

新しいGUI crate、WebView、.NET、download、UAC昇格は追加していない。
Windows USER/GDIと既存Rust依存を使用し、system DLLをpayloadへコピーしない。
GUI manifestは`asInvoker/uiAccess=false/PerMonitorV2`。これは通常user VM・署名の受入の代わりではない。
[IsDialogMessage](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-isdialogmessagea)
によるTab/標準controlのkeyboard処理、DPI変更時のlayout/font再計算を実装する。
screen-reader全体、異なるDPIの実monitor、Linux/macOS/ARMは別途確認が必要。
WebView案へ変える場合は[Runtime配布・更新条件](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)を再評価する。
追加runtimeのper-user導入可否を、managed端末でも管理者不要という保証へ読み替えない。
既存Rust/CRT/CPython/wheelのnotices/source/SBOM・署名gateも継続する。

GUI実行検証の初回失敗は削除しない。handshake前にcontrolを操作したtest競合、
他processのEDITをGetWindowText/SetWindowTextで扱ったtest API誤用を修正し、WM_GETTEXT/WM_SETTEXTへ変更した。
前者は画面表示前からcontrolをdisableする実装も追加した。
実payload更新の45秒test期限ではstage2,052/2,298filesで中断し、旧activeとdata保持を確認した。
同期保存を省かず、後続検証はfresh root・120秒期限で再開する。
前段のlegacy fixtureの長いpath失敗と短いtemp root再実行も上の記録に保持する。
最終normal buildの同等出力n=5はplain3,874.649ms/profile3,825.600ms、interpreter noop26.865ms。
全file hash＋path検査1,290.918ms、inventory1,170.533ms、import971.768ms、
CLI/doctor261.487ms、worker wall1,346.111ms（子の内訳を含む）。
[全sampleと条件](../../native/setup/results/windows-x64-20261023/launch-costs.json)、
[source/artifact identity・build flags・検証範囲](../../native/setup/results/windows-x64-20261023/verification.json)を保存する。
plain/profile差や旧4.13秒との差を改善率として扱わない。検証を省く変更はない。

normal engine/GUIのexe単体は809,984/523,776 bytes。
PE importsはengineがKERNEL32/ntdll/API-set、GUIがさらにUSER32/GDI32で、VCRUNTIME importはない。
これはPython/runtime/notices/署名込みの配布sizeでも、標準user VMの導入実証でもない。

最終GUIは6tests成功（47.25秒）。専用Python実payloadでの導入・更新・rollback、
CLI plan/result一致、確認前の無変更、入力変更時の再plan、日本語path、起動直後取消、
after-replace crash→status→確認付きrepair、stale plan、不正JSON/過大出力＋2MiB floodの切断復帰を含む。
GUIのcallback再入はRefCellで重複する可変参照を避け、再入Closeを保持する。
summaryは検査済みengine値の表示だけで、raw JSONも参照できる。
Windows 96DPIのPNGを目視確認し、native Tab(root→manifest)、Spaceでの確認、
確認欄→実行buttonのTab順をposted keyで確認した。物理keyboard、Narrator、別DPI monitorの認証とは呼ばない。

共通streamingの4tests成功（107.04秒）は出力停滞中のcommit/statusも含む。
その後の同範囲に影響する変更はないため再利用し、最終mainで追加した空/過長request ID早期拒否は
別の2casesでbounded error・無変更を確認する。
normal buildでは実candidateと孫processを生存中handleで保持し、取消後の両方の終了、
既存jobの版固定・更新・rollback・破損拒否を2testsで確認した（84.40秒）。
normal Rust unitはengine3件・GUI client2件成功。全pytest成功へ集約しない。
既存Python処理本体とresource/data codeは前段から変更せず、関係のないsource全回帰は再実行しない。

独立レビューは出力待ちとcommit lockの分離、stdin前取消保存、invalid出力後のpipe閉鎖、
callback再入・Close guard、plan確認/入力変更、計測の内包関係を確認し、未解消P0/P1/P2は0件。
reviewerはcode/保存証拠の確認であり、実行担当はprimary。原本は作業領域
`evidence/native-gui-review.md`、JUnitと画面は同`evidence/`に保持する。

## 次のgate

- P0: 最終比較のbuild provenance/同等出力を保存済み。署名/GUI/他OS/実payloadの言語比較は残る。
- P1: source回帰に加えinstalled recovery・版固定・export保護を確認。legacy migration handoffの自動化は後続。
- P2: production root選択、Windows owner/DACL検査、TOCTOU/hardlink対策、process-tree停止、
  署名済み標準user VM、全fault/disk-full/AV lock/電源断/自己更新/削除の受入が残る。
- 配布: VEP AGPL対応source/build材料、CPython/wheel/native依存のexact notices/SBOM、
  FFmpeg/ASR/CUDA/model条件、code-sign key/署名を揃える。鍵発行・購入・OS trust/ACL変更・配布は本作業で実行しない。
- P3/P4: 開発GUI以降のaccessibility/OS受入、TUF/client/key運用/撤回policy、任意component、Linux/macOS/ARM。

設計上の方針やローカルの成功を、本番対応・再配布可能・release-readyと読み替えない。

## Windows user installer continuation checkpoint — 2026-10-24

Local worktree `vep-windows-installer`, branch `codex/windows-user-installer`,
baseline main `809b352cd471721b1fc4e769880c958c48902705`.
This continuation is local implementation/testing/package preparation only.
No push, PR, merge, release, production profile installation, signing credentials,
account changes, browser changes, or existing ACL repairs are performed.
It is paused at the user's request to permit an application restart; it is not
distribution-ready or complete production acceptance.

Implemented, with integration still pending:

- Read-only Known Folder discovery, explicit `--user-root`, normal-token checks,
  protected current-user/SYSTEM/Administrators DACL at NEW root creation only.
  Existing roots require verified ownership/private ACL and are never repaired.
- Separate user marker/root identity and resumable initialization; mode/root/
  installation identity/generation are bound to plans. All versions/data retained.
- Shared CLI/GUI user mode, explicit confirmation/license acceptance, bounded
  license-text pages, receipt-bound retained engine/GUI/launcher preparation.
  End-to-end installed entrypoint and GUI execution are NOT yet validated.
- Directory traversal handles and no-follow final file handles, single-link file
  checks, common state/payload/lock adapters, private state/program inventory checks.
  Directory handles deny DELETE, allowing child writes/atomic replacement.
  This is not a claim of protection from malicious current-SID metadata changes
  or administrators. Adversarial/race/fault acceptance remains pending.
- Windows reserved/trailing-dot release IDs are rejected. Direct Win32 paths and
  Python context paths use explicit extended paths; actual long-root runtime
  acceptance remains pending, without registry/security-policy changes.
- A pinned notice-source catalog and opt-in local notice collector preserve full
  runtime/wheel/native/toolchain notices and upstream SBOM evidence. The collector
  has NOT completed; the final Windows payload/SBOM/bundle is NOT prepared.

Verified checkpoint scope:

- Current Windows process token is unelevated, Administrators not enabled.
  This is not a clean standard-user account/VM without developer tools.
- Fault-injection debug build passes. Rust existing units: engine 3, GUI client 2.
- `tests/test_user_install.py`: 6 passed, latest 0.90 seconds. Fresh isolated roots
  cover confirmation, repeated initialization/data retention, interruption at both
  initialization markers, existing non-private/foreign-root unchanged rejection,
  moved-marker identity rejection, and hardlinked state rejection.
- Initial root tests failed 5/6 because marker replacement returned Windows
  sharing violation 32. Parent directory share-write was necessary for child
  atomic replacement. After correction all 6 passed; prior failed evidence is kept.
- Ruff focused check/format, Cargo formatting, diff whitespace pass. No full source
  pytest, real user-mode runtime update/rollback/repair, process concurrency/race,
  final normal-build GUI integration, disk-full/AV/power-loss or signed VM pass claim.

Known resume issues:

1. Proposed default `%LOCALAPPDATA%\VEP` collides with an older, unowned `models`
   directory on this host (no installer marker). It was read only and must remain
   untouched. Select a separate owned namespace or explicitly model coexistence
   before testing any default-location installation. Current isolated roots work.
2. Notice collection stops on `r-efi 5.2.0`: no standalone notice file matched the
   collector. It is an upstream all-target pydantic-core dependency, not established
   Windows linkage. Inspect the verified cached archive and handle its actual
   license material honestly; do not silently drop it or call the dossier complete.
3. CPython upstream SPDX and pydantic-core CycloneDX contain other targets; preserve
   them as upstream evidence, not exact Windows static-link inventories. Microsoft
   redistribution qualification/terms and remaining linkage coverage are open gates.
4. Complete local bundle builder/SPDX file inventory, actual private runtime and
   retained-entrypoint lifecycle, license-view GUI UX and negative/race/fault tests.
   Review the changed security/file-operation implementation independently.
5. Signing/credential decisions and clean general-user environment acceptance are
   pending. Do not create credentials/accounts, change permissions/security settings,
   incur costs, or expand online/OS/publication scope without reporting the boundary.

Reproduction and exact local evidence are in the task's
`evidence/windows-user-installer-resume.md`, `user-root-v1.xml` (initial failures),
`user-root-v2.xml`, `user-root-pause.xml`, and `user-native-cargo-metadata.json`.
Downloaded public license sources and the unfinished dossier remain in task-local
`native-build/license-cache` and `native-build/user-notices-v1`; neither is a release.

## Windows user candidate resume - 2026-10-24

The restart checkpoint above is historical. Local implementation has resumed;
this is still an unsigned candidate, with no publication or real profile install.
The default uses the Windows LocalAppData Known Folder plus
`VideoEvidencePipeline`. An existing older `VEP/models` directory is not adopted,
moved, modified or deleted. Only a nonexistent selected root or a valid existing
VEP user marker may initialize/resume. Even an empty unmarked directory is
rejected; choose a new child directory rather than changing its ACL.

The current layout is:

```text
<root>/.vep-user-install.json       installation identity, bound to canonical root
<root>/app/bin/                    retained setup, GUI and VEP entrypoints
<root>/app/versions/<release-id>/   private Python, packages, resources, tools,
                                  notices, source snapshot and SPDX inventory
<root>/app/staging/                verified candidate preparations
<root>/state/                      selection, receipts, journals, contexts, bindings
<root>/data/jobs/                  mutable user jobs, kept across selection changes
<root>/data/models/                optional user models (none installed here)
```

CLI and Win32 GUI use the same JSONL engine. GUI initialization and application
require review of the target/plan and an explicit confirmation. The license view
reads bounded pages of the exact manifest-bound notice text; viewing a license
invalidates an outstanding mutation confirmation. Apply also requires explicit
license acceptance at the engine boundary. No elevation request, PATH/registry
registration, online updater, FFmpeg, ASR package/model or deletion is included.
The retained initial bootstrap engine is not upgraded in place; a future
bootstrap-engine update protocol is a separate remaining distribution concern.

New roots are prepared in a fresh private sibling with a complete synced marker
before publishing the selected path. Initial active state is atomically replaced.
An interrupted preparation can leave an unreferenced private sibling, which is
retained rather than guessed safe to delete. Entry-point recovery saves the
verified bootstrap receipt before publishing files, uses private temporary files
and renames only complete executables into final names. Existing entrypoints with
different bytes cause a conflict rather than replacement. Metadata/ownership
inspection is separate from content reads so a writer lock does not unnecessarily
prevent status or launching the current version. These changes do not claim
protection against malicious same-SID code or administrators.

`scripts/prepare_windows_notices.py` now completes a conservative dossier with
253 notice files, 21 core wheel distributions and 103 upstream pydantic-core
all-target components. r-efi's complete grant/copyright is preserved from AUTHORS;
wit-bindgen-rt notices absent from its crate are pinned to that crate's recorded
upstream VCS commit. Notice storage names follow the existing restricted payload
path rules. Exact upstream SBOMs are preserved as evidence; their non-Windows
entries are not described as confirmed Windows linkage.

`scripts/build_windows_candidate.py` prepares a local offline fixture from
explicit runtime, packages, native and notice inputs. It checks the official
runtime ZIP hash, wheel RECORD self-consistency and locked name/version,
Windows x64 PE headers, notice hashes/inventory, input/output separation, and
source-snapshot coverage of copied resource files. It excludes the empty uv
preparation lock and refuses untracked runtime sources or unlisted notices.
It adds the VEP source snapshot, per-file SPDX 2.3 inventory, manifest and
top-level checksums. RECORD consistency is not publisher authentication or proof
of the original wheel ZIP hash. Native compile features are not inferred from PE
headers; use the separate build/preparation evidence. A working-tree snapshot is
labeled as such, not a clean commit-addressed release source.

The local fault-injection debug fixtures are never distribution artifacts.
`distribution_ready=false` and `publisher_authenticated=false` remain explicit.
Microsoft redistribution qualification/downstream conditions, exact static
linkage/source coverage, signing, clean standard-user acceptance and bootstrap
upgrade policy remain open. Do not create keys/accounts, repair existing ACLs or
change platform security settings to make a test pass.

Execution results, failed runs, artifact identities and remaining tests are kept
in the task's `evidence/windows-user-installer-resume.md`; independent code review
is in `evidence/windows-user-installer-review.md`. Suite duration is verification
wall time on a shared developer host, not a comparative installation benchmark.

Verified continuation results (separate runs, not an all-pytest success claim):

- Rust unit tests: engine 4 and GUI client 2 passed with fault injection, and
  the same 6 passed without features. A separate optimized `--release
  --no-default-features` build used `-C target-feature=+crt-static`, one job.
- The actual payload lifecycle passed install/update/rollback, retained launcher
  help/doctor, old-job runtime binding/data preservation and entrypoint repair.
- The remaining v4 run passed 16 cases: init/retry, aliases, state hardlinks,
  manifest/SBOM refusal, three bootstrap publication interruptions and cancellation.
  That run also had one genuine long-root candidate failure, retained in evidence.
- The long-root failure was caused by a non-extended context argument to Python.
  Canonicalizing the context for both candidate checks and launches fixed it.
  A subsequent driver failure came from CreateProcess's implicit module search
  limit. Explicit extended application paths passed the complete long Japanese
  root install and installed doctor case. Explorer/all shell support is unverified.
- User-mode Win32 GUI license text, confirmation, common plan/result agreement and
  builder input checks passed 5 cases. Normal optimized binaries passed 2 further
  cases (CLI install/retained help/doctor/GUI status and plan; junction target/ACL
  preservation). Normal GUI cancel/failure/repair passed one additional case.
- Junction refusal can report `FILE_TYPE_INVALID` or `LINK_FORBIDDEN` because
  no-follow handles can expose the reparse object as a different file type. Both
  refusal and the untouched target are asserted. An earlier overly narrow error
  expectation failed and is retained. Cancellation also refused a concurrent
  staging-directory rename while the directory guard was held.
- The generated SPDX document passed the official SPDX 2.3 JSON schema. Focused
  Ruff, UTF-8/newline and whitespace checks pass. Repository validation initially
  failed at sandboxed uv export, then passed offline with the task-local cache in
  the normal execution context; no lock regeneration or ASR synchronization.

Acceptance is limited to the existing unelevated developer host and synthetic
fixtures. It does not certify a clean standard-user machine without toolchains,
SmartScreen/signing, cross-user attack simulation, disk exhaustion, AV contention,
power loss, all GUI accessibility/commit-uncertainty states, or other Windows/CPU
versions. Prior source/main CI and sandbox GUI results are not relabeled as these
new user-install tests. No new performance improvement is claimed.

## Post-candidate verification and packaging corrections - 2026-10-24

The complete Windows suite at local commit `00aac774` finished with **923 passed,
17 skipped and 1 failed** in approximately 54 minutes. The failure was the long
Japanese-root apply driver's 180-second timeout. Read-only inspection found a
complete staging copy, active generation zero, no selected receipt and no
published bootstrap receipt. The failed run is retained; increasing a test budget
does not establish a performance improvement. The follow-up acceptance driver has
a separate 300-second bound. No full-suite success is claimed for that commit.

The follow-up changes keep all file hashes, no-follow handle checks, ownership
and private DACL checks. Directory creation retains one inspected deny-delete
handle per ancestor for the complete walk, avoiding repeated opens of the same
ancestor chains. Permission inventory checks cancellation and reports `CHECK`
progress between files/directories; launcher profiling now records
`private_inventory_ms` separately. Windows invalid characters, reserved device
aliases and the supported 255 UTF-16-unit component limit are rejected before
creating an initialization sibling. These changes do not assert a measured
installation speedup on the shared host.

Audit of the actual copied packages found six generated uv console wrappers with
build-machine interpreter references. VEP invokes `sys.executable -m yt_dlp` and
does not need these wrappers. The builder verifies original input RECORDs, omits
only declared recognizable wrappers from its fresh output, preserves original
RECORDs and their hashes under `packaging/`, rewrites the affected RECORDs and
verifies the output again. Unknown executable artifacts cause refusal. The old
candidate remains unchanged and must not be distributed.

The original embedded Python `._pth` also omitted the private packages directory,
so the actual module subprocess could not import yt-dlp. The copied configuration
now adds only the verified relative `../packages` and `../payload/src` paths,
preserves its original bytes, keeps `site` disabled and records the transformation.
The latter path is required by the production Windows child-process job gate.
Offline version probes cover a hostile
current directory, `PYTHONPATH` and `PYTHONHOME`; no media or network request is
needed. Native builds use ephemeral rustc source-path remapping and are checked
for known build-profile prefixes. Remapping is not a general secret scanner or
publisher attestation.

The first corrected candidate's module probe exposed a further regression:
child Python does not inherit the launcher's `-B`, and added 74 unlisted bytecode
files after initial inventory verification. That candidate is retained as failed
evidence. The yt-dlp and Windows job-gate child commands now explicitly pass
`-B`; acceptance runs on fresh copied or installed private resources, compares
their complete file hashes before/after child execution, and checks the next
native doctor invocation. Original distribution inputs are never executed by
these probes. Source-mode subprocess behavior retains the same acquisition
arguments and result contract apart from suppressing bytecode writes.

The top-level SPDX document is an exact file inventory, not a resolved component
dependency graph. Conservative upstream SBOMs and notices remain supplementary
evidence. Exact target-specific linkage/corresponding-source closure and the
distributor's Microsoft runtime/compiler redistribution rights require further
evidence and owner review; their absence is not a confirmed blanket dependency
incompatibility or legal clearance. No FFmpeg, ASR runtime or model is bundled.

Processing-version updates do not service the native bootstrap: the first
`state/bootstrap.json` receipt continues to select the retained engine/GUI bytes.
Repair also verifies that original version's whole payload, so damage to unused
original processing files can block recovery. Supported native engine handoff,
minimum-version policy, replacement/rollback, retention and uninstall are not yet
implemented or accepted. Legacy source-mode jobs are not automatically migrated;
installed private-job pinning tests are a narrower compatibility guarantee.

These binary servicing, license evidence, signing/SmartScreen and clean-host
acceptance gaps block a supported binary publication. They should be explicit in
a source/tests/docs-only Draft review, which conveys no compiled dependencies.
External source-sharing authorization is separately pending. Test artifacts stay
local; no default profile installation, security/permission change, push, PR or
release is performed by this continuation.

The bounded follow-up checks completed on the same unelevated Windows host:

| Scope | Result |
| --- | --- |
| Candidate transforms, acquisition and process-tree regression | 50 passed, 1 unavailable-symlink skip |
| Native transaction and streaming regression | 12 passed; 6 GUI cases initially not enabled |
| Explicit GUI transaction/protocol regression | All 6 previously unenabled GUI cases passed |
| User-root lifecycle, refusal, interruption, cancellation and long-path acceptance | 41 passed |
| Optimized no-fault CLI/GUI and junction/data preservation | 4 passed |

Both normal and fault-enabled Rust unit sets passed 6 cases each. All-target,
all-feature Clippy with warnings denied, Cargo formatting, Ruff check/format of
all 142 Python files, repository validation, schema validation and changed-file
UTF-8/whitespace checks passed. No separate Python static type-check suite is
configured; Rust checks include compilation/type checking. The full 941-case
suite was not restarted after these corrections: unaffected Python/media results
are reused from the frozen run, and its failed long-path case was explicitly
rechecked. These scoped results do not turn the original full run into a pass.
