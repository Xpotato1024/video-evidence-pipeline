FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04@sha256:ebef3c171eeef0298e4eb2e4be843105edf3b8b0ac45e0b43acee358e8046867

ENV DEBIAN_FRONTEND=noninteractive \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/vep
COPY containers/asr/requirements.lock /tmp/asr-requirements.lock
RUN python3 -m pip install --break-system-packages --no-cache-dir -r /tmp/asr-requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN python3 -m pip install --break-system-packages --no-cache-dir --no-deps .

WORKDIR /workspace
ENTRYPOINT ["vep"]
