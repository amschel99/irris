#!/usr/bin/env bash
# Container entry point. Starts the CPU vision model (Ollama, on 127.0.0.1 only) next to Irris, so camera checks
# work without a GPU. When VLM_BASE_URL points at a GPU, Irris uses that first and this one only as a fallback.
# If either process dies, the container exits so Railway restarts it.
set -Eeuo pipefail
log() { printf '[start] %s\n' "$*" >&2; }

PORT="${PORT:-8000}"
MODEL="${VLM_FALLBACK_MODEL:-qwen3.5:2b}"
export OLLAMA_HOST=127.0.0.1:11434 OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}" OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-1}"

if [[ "${LOCAL_OLLAMA:-1}" != "1" ]]; then
  log "LOCAL_OLLAMA is off: no CPU fallback model"
  exec python -m irris
fi

ollama serve &
OLLAMA_PID=$!
APP_PID=""
trap 'kill -TERM "$APP_PID" "$OLLAMA_PID" 2>/dev/null || true; wait || true; exit 0' TERM INT

for _ in $(seq 1 90); do
  curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
  kill -0 "$OLLAMA_PID" 2>/dev/null || { log "ollama exited during startup"; exit 1; }
  sleep 1
done
log "ollama is up; warming $MODEL in the background"
( curl -fsS --max-time 600 http://127.0.0.1:11434/api/generate \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_predict\":1}}" \
    >/dev/null 2>&1 && log "$MODEL warmed" || log "warm-up failed; the first check will load it" ) &

python -m irris &
APP_PID=$!
set +e
wait -n "$OLLAMA_PID" "$APP_PID"
code=$?
set -e
log "a process exited (code $code); stopping so the container restarts"
kill -TERM "$APP_PID" "$OLLAMA_PID" 2>/dev/null || true
wait 2>/dev/null || true
exit $(( code == 0 ? 1 : code ))
