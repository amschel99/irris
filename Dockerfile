# Irris: the dashboard, the server, and a CPU vision model (Ollama + Qwen) as the fallback when no GPU is set.
#
# Railway has no GPUs. Set VLM_BASE_URL to a GPU box (e.g. Ollama behind a tunnel) and Irris uses it first; when
# it's unreachable, or not set, camera checks run on the CPU model baked into this image (~20-40 s per check).

# ---------------------------------------------------------------- 1. the dashboard
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------- 2. the server
FROM python:3.12-slim
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates zstd tini libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Ollama (pinned, checksum-verified, CPU only: the GPU backends are removed)
ARG OLLAMA_VERSION=0.40.6
ARG OLLAMA_SHA256=c164ab8b1b28e2a247c29719b8e404c423c5b83ae64c3ab4e59d50c7ff7fc5e0
RUN curl -fsSL -o /tmp/ollama.tar.zst \
      "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst" \
 && echo "${OLLAMA_SHA256}  /tmp/ollama.tar.zst" | sha256sum -c - \
 && zstd -d -c /tmp/ollama.tar.zst | tar -x -C /usr \
 && rm /tmp/ollama.tar.zst \
 && rm -rf /usr/lib/ollama/cuda_* /usr/lib/ollama/rocm* /usr/lib/ollama/vulkan* /usr/lib/ollama/mlx* \
 && test -x /usr/bin/ollama

# The CPU vision model, baked in so the container never downloads at runtime
ARG VLM_FALLBACK_MODEL=qwen3.5:2b
ENV VLM_FALLBACK_MODEL=${VLM_FALLBACK_MODEL} OLLAMA_MODELS=/opt/ollama/models OLLAMA_HOST=127.0.0.1:11434
RUN ollama serve >/tmp/ollama.log 2>&1 & pid=$!; \
    for i in $(seq 1 60); do curl -sf http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done; \
    ollama pull "${VLM_FALLBACK_MODEL}" || { cat /tmp/ollama.log; exit 1; }; \
    kill "$pid"; wait "$pid" 2>/dev/null || true; rm -f /tmp/ollama.log

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOCAL_OLLAMA=1 HOST=0.0.0.0 PORT=8000
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY irris/ irris/
COPY site/ site/
COPY docs/ docs/
COPY scripts/ scripts/
COPY --from=web /web/dist web/dist
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "/srv/scripts/start.sh"]
