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


@pytest.fixture()
def db(tmp_path):
    from irris import db as dbmod
    url = os.environ.get("IRRIS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    d = dbmod.Database(url)
    if d.pg:   # a shared Postgres: start clean
        for t in ("users", "workspaces", "members", "invites", "sessions", "api_keys", "records", "passages",
                  "sources", "files", "cameras", "runs", "conversations", "messages"):
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
