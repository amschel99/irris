# Irris

The AI twin for manufacturing and engineering teams. A team connects its data, procedures and cameras. Irris
learns how they work, answers questions with citations, queries their databases, watches the work through a phone
or factory camera, guides people step by step, checks each step against the SOP, logs readings and writes the
batch record.

| Path | What |
|---|---|
| `/` | The website (`site/`) |
| `/app` | The dashboard (`web/`, React + Vite) |
| `/api/v1` | The REST API (`irris/api.py`), reference at `/api/docs` |
| `/mcp` | The MCP server (`irris/mcp.py`) |
| `/developers`, `/standard`, `/llms.txt` | Developer docs, the data standard, the LLM guide |

## What's inside

- **Accounts and workspaces:** sign-up, sign-in, workspaces with owner/admin/member roles, invite links, API keys
  (`irr_…`, hashed).
- **IRF, the Irris Record Format** ([docs/IRF.md](docs/IRF.md)): one JSON envelope for any data. IRF, any JSON, NDJSON,
  files (PDF with pages, Word, Excel, CSV, JSON, text, images), webhooks and Postgres all become records. Same
  `source` + `id` upserts.
- **Search:** Postgres full-text search (GIN index), or SQLite FTS5 on a laptop. Answers cite the record and page.
- **The assistant** (`irris/agent.py`): any OpenAI-compatible model with tool calling (DeepSeek by default). Tools:
  search, records, databases, read-only SQL, look through a camera, procedures, start a guided run, remember a
  lesson, log a record. Without a model it still answers from search.
- **Training:** team description, standing instructions, lessons (corrections it always applies), procedures, and a
  JSONL export of conversations, lessons and procedures for fine-tuning.
- **Cameras:** pair any phone by link or QR (no account on the phone), or add an HTTP snapshot, MJPEG or RTSP
  camera. The vision model is any OpenAI-compatible API that takes images (Ollama or vLLM on a GPU, or hosted).
- **Guided runs** (`irris/runs.py`): step-by-step checks through the camera or by hand, readings against limits,
  deviations, and a `batch_record` when the run ends.
- **Connected Postgres** (`irris/connectors.py`): schema, table sync, read-only queries (single SELECT/WITH only,
  READ ONLY transaction, 15 s timeout). Connection strings and model keys are encrypted with `SECRET_KEY`.

## Run it locally

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Linux/macOS: .venv/bin/pip
cd web && npm install && npm run build && cd ..
cp .env.example .env                               # add LLM_API_KEY, and a VLM_BASE_URL for camera checks
.venv/Scripts/python -m irris                      # http://localhost:8000
```

SQLite in `./data` is used when `DATABASE_URL` is unset. For dashboard development, run `npm run dev` in `web/`
(it proxies `/api` and `/mcp` to port 8000).

Phone cameras need HTTPS (browsers only allow the camera on a secure page), so pair phones against the deployed
URL, or a tunnel (`cloudflared tunnel --url http://localhost:8000`).

## Deploy on Railway

1. New project → Deploy from GitHub repo → this repo. Railway builds the `Dockerfile` (`railway.json`).
2. Add a Postgres database and set `DATABASE_URL=${{Postgres.DATABASE_URL}}` on the service.
3. Set `SECRET_KEY` to a long random string (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   Keep it: stored connection strings and model keys are encrypted with it.
4. Set `LLM_API_KEY` (DeepSeek, or point `LLM_BASE_URL`/`LLM_MODEL` at another provider).
5. For camera checks, set `VLM_BASE_URL`/`VLM_MODEL` (and `VLM_API_KEY` if the provider needs one). Railway has no
   GPUs, so point it at a GPU box (Ollama behind a tunnel) or a hosted vision model. Each workspace can also set
   its own models in Settings → Models.
6. Generate a domain. The health check is `/api/v1/health`.

## Tests

```bash
.venv/Scripts/python -m pytest -q                 # SQLite
IRRIS_TEST_DATABASE_URL=postgresql://… pytest -q  # Postgres (the suite clears its tables first: use a scratch DB)
```
