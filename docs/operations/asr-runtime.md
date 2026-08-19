---
schema_version: "1.1"
document_id: VEP-OPS-ASR
title: "ASR runtime・model・device運用"
document_type: operations
status: active
canonical: true
canonical_for: asr-runtime-operations
owners:
  - Xpotato1024
supersedes: []
superseded_by: null
related_documents:
  - VEP-ARCH-PIPELINE
  - VEP-ARCH-TECH
  - VEP-ARCH-TRUST
  - VEP-OPS-CLI-LOCAL
---
# ASR runtime・model・device運用

## 1. 正本と標準profile

ASRのengine、runtime、model repository、immutable revision、compute type、VRAM threshold、
container imageは[`../../config/asr-profiles.yaml`](../../config/asr-profiles.yaml)が正本です。
標準`whisper-large-v3` profileはfaster-whisper／CTranslate2、`large-v3`、GPU `float16`、
CPU `int8`、`auto` device policyを使用します。operatorやagentが任意model名、revision、
runtime package、model directoryを通常transcriptionへ渡す経路はありません。

## 2. Buildとprovision

repository rootでDockerと、GPUを使う場合はNVIDIA Container Toolkitを準備し、次を実行します。

```bash
uv sync --locked --all-groups
uv run vep asr image-build --profile whisper-large-v3
uv run vep asr model-provision --profile whisper-large-v3
```

`image-build`はCUDA 12.6.3／cuDNN 9 runtimeのdigest固定NVIDIA base、
[`../../containers/asr/Dockerfile`](../../containers/asr/Dockerfile)、exact Python runtime／build requirements、
VEP source/configのbuild-contract fingerprintからprofile指定のimageを作ります。source、config、dependency、
Dockerfile等のbuild contractが変わった場合は再buildが必要です。
推論／provisionはcontract labelを照合し、immutable image IDを`--pull never`で起動します。stale／unknown imageは
自動pullせず停止します。`model-provision`だけがmodel取得のnetworkを利用し、profileに
pinされたHugging Face repository／revisionをcontainer内のnoncanonical OS temp stagingへ取得し、container終了後に
hostがlink／file count／size／required files／payload hashを検証してから
`.local/models/asr/<profile>/<revision>/model`へpublishします。同じrepository／revisionは再利用できます。
完了時は隣接する`manifest.json`へmodel identity、revision、storage fingerprint、file count、size、
provision timeを保存します。Hugging Faceの`.cache/huggingface` control metadataはpublication前に除去し、
model storage identityは実model payloadのrelative pathとcontent SHA-256だけから計算します。

## 3. 推論

通常のpipeline commandだけを使用します。

```bash
uv run vep transcribe .local/jobs/JOB \
  --preferred-language ja \
  --asr-profile whisper-large-v3 \
  --asr-device auto
```

public CLIのhost processがJobWorkspace、caption選択、coverage／gap計算、worker response検証、segment統合、
canonical artifact／manifest／transition／lock／permissionを所有します。captionだけで完結するjobはDocker executableも
modelも不要です。不足区間がある場合だけASR workerを`--pull never --network none`で起動し、exact speech audioと
modelをread-only、typed requestをread-onlyでmountします。canonical `.local/jobs/<job>`やjob rootをcontainerへ
read-write mountしません。worker stdoutはbounded typed JSONとしてhostが再検証してからpublishします。
inference中のmodel download、package install、cloud fallbackはできません。
profileまたはprovision manifestと一致するmodelがなければ`ASR_MODEL_NOT_PROVISIONED`で停止し、
`vep asr model-provision`を案内します。caption coverageが十分なjobはmodelをloadしません。

## 4. Device policy

- `auto`: container GPU passthrough、CTranslate2 CUDA device、configured `float16` supportを確認し、
  profile threshold以上のfree VRAMを持つGPUだけを候補にします。複数候補ではfree VRAM最大、同値なら
  index最小を選びます。該当なしはCPU `int8`です。
- `cpu`: GPU probe自体を行わず、常にCPU `int8`を使用します。
- `cuda`: usable GPUから決定的に1台を選び、利用不能なら停止します。
- `cuda:<index>`: 指定indexだけを使用し、利用不能なら停止します。

標準profileのthresholdは6144 MiBです。割合ではなくASR開始時点のabsolute free VRAMを使用します。
`auto`でmodel loadまたはinferenceがCUDA OOM／allocation failure等のresource failureになった場合だけ、
CPUへ1回fallbackします。invalid input、corrupted model、schema／filesystem／programming errorはfallbackせず
失敗します。explicit CUDAはresource failureでもCPUへ黙って変更しません。

## 5. Provenance

profile指定のengine／runtime versionとinstalled versionが一致しないimageは拒否します。ASR backend identityは
engine／version、runtime version、model／revision／実fileと照合済みstorage identity、device policy、
selected device、stable selection reason、candidate／selected GPU index／name／total／selection時free VRAM、
configured threshold、compute type、container image／digest、fallback有無／元device／理由をtranscript manifestへ
記録します。profiled ASR recordではproduction provenanceを必須とし、legacy manifestだけをnullable互換で読みます。

## 6. Dependency lock

ASR runtime／build dependencyの正本は`pyproject.toml`と`uv.lock`の`asr-container` groupです。
`containers/asr/requirements.lock`は次で決定的に生成し、repository validatorが同じcommandの出力と完全一致を検査します。

```bash
uv export --locked --group asr-container --no-dev --no-emit-project \
  --no-annotate --no-hashes --no-header --format requirements-txt \
  --output-file containers/asr/requirements.lock
uv run python scripts/validate_repository.py --root .
```

Docker buildはlockを先にinstallし、VEP packageを`--no-build-isolation --no-deps`でinstallします。

## 7. Troubleshooting

- `ASR_CONTAINER_RUNTIME_UNAVAILABLE`: Dockerをinstallしてdaemonを起動します。ad-hoc Python環境へ
  faster-whisperをinstallして迂回しません。
- `ASR_MODEL_NOT_PROVISIONED`: `vep asr model-provision --profile ...`を明示実行します。
- `ASR_DEVICE_UNAVAILABLE`: `cuda`／`cuda:<index>`のpassthrough、driver、CTranslate2 capabilityを確認します。
  CPUを意図する場合だけ`--asr-device cpu`を明示します。
- `ASR_CONTAINER_TRANSCRIPTION_FAILED`: container出力、profile、model manifestを確認します。inferenceへ
  networkを追加したり、別modelを一時downloadしたりしません。

通常PR CIはimageを実buildし、Python、faster-whisper／CTranslate2 version、cuBLAS、cuDNN 9、worker CLI、
build-contract labelをnetwork-disabled smokeで検査します。large-v3 provision、CPU実model inference、GPU smokeは
hardware／network依存のmanual integrationとして区別します。
