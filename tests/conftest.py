import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ["LLM_API_KEY"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["VLM_BASE_URL"] = ""
os.environ.setdefault("IRRIS_WORKER", "0")        # tests run background jobs explicitly (jobs.run_pending)
os.environ.setdefault("IRRIS_EMBEDDINGS", "off")  # and use a fake embedder when they need one


@pytest.fixture()
def db(tmp_path):
    from irris import db as dbmod
    url = os.environ.get("IRRIS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    os.environ["FILES_DIR"] = str(tmp_path / "files")
    d = dbmod.Database(url)
    if d.pg:   # a shared Postgres: start clean
        for t in ("users", "workspaces", "members", "invites", "sessions", "api_keys", "records", "passages",
                  "sources", "files", "cameras", "runs", "conversations", "messages",
                  "jobs", "stored_files", "doc_pages", "embeddings", "memory_items", "memory_events",
                  "conv_summaries", "llm_calls", "ws_limits",
                  "media_settings", "camera_state", "watch_rules", "media_jobs", "observations", "camera_rois",
                  "shift_summaries"):
            d.execute(f"DELETE FROM {t}")
    dbmod.set_db(d)
    yield d
    dbmod.set_db(None)


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from irris.app import create_app
    from irris import auth
    auth._attempts.clear()
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def signed_in(client):
    r = client.post("/api/v1/auth/signup", json={"email": "ada@plant.co", "password": "correct horse",
                                                 "name": "Ada", "workspace": "Line 3", "industry": "injection moulding"})
    assert r.status_code == 200, r.text
    return client
