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

## 2. 初回setup

repository rootでDockerと、GPUを使う場合はNVIDIA Container Toolkitを準備し、次を実行します。

```bash
uv sync --locked --all-groups
uv run vep asr image-build --profile whisper-large-v3
uv run vep asr model-provision --profile whisper-large-v3
```

`image-build`はdigest固定のCUDA base、[`../../containers/asr/Dockerfile`](../../containers/asr/Dockerfile)、
exact Python requirements、VEP source/configのbuild-contract fingerprintからprofile指定のimageを作ります。
推論／provisionはcontract labelを照合し、immutable image IDを`--pull never`で起動します。stale／unknown imageは
自動pullせず停止します。`model-provision`だけがmodel取得のnetworkを利用し、profileに
pinされたHugging Face repository／revisionを`.local/models/asr/<profile>/<revision>/model`へ取得します。
完了時は隣接する`manifest.json`へmodel identity、revision、storage fingerprint、file count、size、
provision timeを保存します。modelをOS Tempへ永続化しません。

## 3. 推論

通常のpipeline commandだけを使用します。

```bash
uv run vep transcribe .local/jobs/JOB \
  --preferred-language ja \
  --asr-profile whisper-large-v3 \
  --asr-device auto
```

public CLIはASR containerを`--pull never --network none`で起動し、job rootをread-write、configとmodel storageを
read-only mountします。inference中のmodel download、package install、cloud fallbackはできません。
profileまたはprovision manifestと一致するmodelがなければ`ASR_MODEL_NOT_PROVISIONED`で停止し、
`vep asr model-provision`を案内します。caption coverageが十分なjobはmodelをloadしません。

## 4. Device policy

- `auto`: container GPU passthrough、CTranslate2 CUDA device、configured `float16` supportを確認し、
  profile threshold以上のfree VRAMを持つGPUだけを候補にします。複数候補ではfree VRAM最大、同値なら
  index最小を選びます。該当なしはCPU `int8`です。
- `cpu`: 常にCPU `int8`を使用し、GPUへ変更しません。
- `cuda`: usable GPUから決定的に1台を選び、利用不能なら停止します。
- `cuda:<index>`: 指定indexだけを使用し、利用不能なら停止します。

標準profileのthresholdは6144 MiBです。割合ではなくASR開始時点のabsolute free VRAMを使用します。
`auto`でmodel loadまたはinferenceがCUDA OOM／allocation failure等のresource failureになった場合だけ、
CPUへ1回fallbackします。invalid input、corrupted model、schema／filesystem／programming errorはfallbackせず
失敗します。explicit CUDAはresource failureでもCPUへ黙って変更しません。

## 5. Provenance

profile指定のengine／runtime versionとinstalled versionが一致しないimageは拒否します。ASR backend identityは
engine／version、runtime version、model／revision／実fileと照合済みstorage identity、device policy、
selected device、GPU index／name、total／selection時free VRAM、configured threshold、compute type、container
image／digest、fallback有無／元device／理由をtranscript manifestへ記録します。取得不能な値はnullableで、
推測値を入れません。

## 6. Troubleshooting

- `ASR_CONTAINER_RUNTIME_UNAVAILABLE`: Dockerをinstallしてdaemonを起動します。ad-hoc Python環境へ
  faster-whisperをinstallして迂回しません。
- `ASR_MODEL_NOT_PROVISIONED`: `vep asr model-provision --profile ...`を明示実行します。
- `ASR_DEVICE_UNAVAILABLE`: `cuda`／`cuda:<index>`のpassthrough、driver、CTranslate2 capabilityを確認します。
  CPUを意図する場合だけ`--asr-device cpu`を明示します。
- `ASR_CONTAINER_TRANSCRIPTION_FAILED`: container出力、profile、model manifestを確認します。inferenceへ
  networkを追加したり、別modelを一時downloadしたりしません。

実GPU／CPU smokeはhardware-dependentで通常CIの必須条件にしません。CIはprofile、device decision、
resource-only fallback、offline mount command、provenanceをmock／static testで検証します。
