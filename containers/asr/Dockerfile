FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356

ARG VEP_ASR_CONTRACT_SHA256
ENV DEBIAN_FRONTEND=noninteractive \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VEP_ASR_INTERNAL_RUNTIME=1 \
    VEP_ASR_CONTRACT_SHA256=${VEP_ASR_CONTRACT_SHA256}

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/vep
COPY containers/asr/requirements.lock /tmp/asr-requirements.lock
RUN python3 -m pip install --break-system-packages --no-cache-dir -r /tmp/asr-requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN python3 -m pip install --break-system-packages --no-cache-dir --no-build-isolation --no-deps .

WORKDIR /workspace
ENTRYPOINT ["vep"]
