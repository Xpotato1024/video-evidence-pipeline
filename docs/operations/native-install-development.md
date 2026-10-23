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
| GUI共有 | versioned UTF-8 JSONLのplan/apply/status/errorを同engineへ委譲できる。現実装は1request/process。GUI、progress、取消、accessibilityは未実装 |
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

## 次のgate

- P0: 最終比較のbuild provenance/同等出力を保存済み。署名/GUI/他OS/実payloadの言語比較は残る。
- P1: source回帰に加えinstalled recovery・版固定・export保護を確認。legacy migration handoffの自動化は後続。
- P2: production root選択、Windows owner/DACL検査、TOCTOU/hardlink対策、process-tree停止、
  署名済み標準user VM、全fault/disk-full/AV lock/電源断/自己更新/削除の受入が残る。
- 配布: VEP AGPL対応source/build材料、CPython/wheel/native依存のexact notices/SBOM、
  FFmpeg/ASR/CUDA/model条件、code-sign key/署名を揃える。鍵発行・購入・OS trust/ACL変更・配布は本作業で実行しない。
- P3/P4: 同engineのGUI、TUF/client/key運用/撤回policy、任意component、Linux/macOS/ARM。

設計上の方針やローカルの成功を、本番対応・再配布可能・release-readyと読み替えない。
