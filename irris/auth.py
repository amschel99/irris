"""Who is calling: a signed-in person (session cookie or bearer session token) or a workspace API key.

People belong to workspaces with a role (owner, admin, member). Dashboard calls name the workspace in the
`X-Workspace` header; API keys belong to one workspace, so API calls don't need it.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request

from irris.db import Database, get_db, new_id, now
from irris.security import check_password, hash_password, sha256, token

COOKIE = "irris_session"
SESSION_TTL = 30 * 24 * 3600
KEY_PREFIX = "irr_"
ROLES = ("owner", "admin", "member")


@dataclass
class Ctx:
    db: Database
    workspace: dict
    role: str
    user: dict | None = None
    via: str = "session"

    @property
    def ws_id(self) -> str:
        return self.workspace["id"]

    @property
    def user_id(self) -> str:
        return self.user["id"] if self.user else ""

    def require_admin(self) -> None:
        if self.role not in ("owner", "admin"):
            raise HTTPException(403, "only workspace admins can do that")


# ---------------------------------------------------------------- brute-force guard
_attempts: dict[str, deque] = defaultdict(deque)


def too_many(key: str, limit: int = 10, window: float = 600) -> bool:
    q = _attempts[key]
    t = time.time()
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


# ---------------------------------------------------------------- users and sessions
def user_api(u: dict) -> dict:
    return {"id": u["id"], "email": u["email"], "name": u["name"], "created": u["created"]}


def create_user(db: Database, email: str, password: str, name: str) -> dict:
    email = email.strip().lower()
    if "@" not in email or len(email) > 200:
        raise HTTPException(400, "enter a valid email")
    if len(password) < 8:
        raise HTTPException(400, "the password needs at least 8 characters")
    if db.one("SELECT id FROM users WHERE email = ?", [email]):
        raise HTTPException(409, "an account with that email already exists. Sign in instead.")
    uid = new_id()
    db.execute("INSERT INTO users (id, email, name, pw_hash, created) VALUES (?,?,?,?,?)",
               [uid, email, name.strip()[:120], hash_password(password), now()])
    return db.one("SELECT * FROM users WHERE id = ?", [uid])


def login(db: Database, email: str, password: str) -> dict:
    u = db.one("SELECT * FROM users WHERE email = ?", [email.strip().lower()])
    if not u or not check_password(password, u["pw_hash"]):
        raise HTTPException(401, "wrong email or password")
    return u


def new_session(db: Database, user_id: str) -> str:
    raw = token("irs_", 32)
    db.execute("INSERT INTO sessions (token_hash, user_id, created, expires) VALUES (?,?,?,?)",
               [sha256(raw), user_id, now(), now() + SESSION_TTL])
    return raw


def end_session(db: Database, raw: str) -> None:
    db.execute("DELETE FROM sessions WHERE token_hash = ?", [sha256(raw)])


def session_user(db: Database, raw: str) -> dict | None:
    if not raw:
        return None
    row = db.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires > ?",
                 [sha256(raw), now()])
    return row


def workspaces_of(db: Database, user_id: str) -> list[dict]:
    rows = db.execute("SELECT w.id, w.name, w.industry, m.role FROM members m JOIN workspaces w ON w.id = m.workspace_id "
                      "WHERE m.user_id = ? ORDER BY m.created", [user_id])
    return rows


def create_workspace(db: Database, user_id: str, name: str, industry: str = "", about: str = "") -> dict:
    name = name.strip()[:120] or "My team"
    wid = new_id()
    with db.transaction() as tx:
        tx.execute("INSERT INTO workspaces (id, name, industry, about, created) VALUES (?,?,?,?,?)",
                   [wid, name, industry.strip()[:120], about.strip()[:4000], now()])
        tx.execute("INSERT INTO members (workspace_id, user_id, role, created) VALUES (?,?,?,?)",
                   [wid, user_id, "owner", now()])
    return db.one("SELECT * FROM workspaces WHERE id = ?", [wid])


# ---------------------------------------------------------------- API keys
def create_key(db: Database, workspace_id: str, name: str) -> dict:
    raw = token(KEY_PREFIX, 30)
    kid = new_id()
    db.execute("INSERT INTO api_keys (id, workspace_id, name, prefix, key_hash, created) VALUES (?,?,?,?,?,?)",
               [kid, workspace_id, name.strip()[:80] or "API key", raw[:10], sha256(raw), now()])
    return {"id": kid, "name": name, "key": raw, "prefix": raw[:10]}


def key_workspace(db: Database, raw: str) -> dict | None:
    row = db.one("SELECT k.id AS key_id, w.* FROM api_keys k JOIN workspaces w ON w.id = k.workspace_id "
                 "WHERE k.key_hash = ?", [sha256(raw)])
    if row:
        db.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", [now(), row.pop("key_id")])
    return row


# ---------------------------------------------------------------- request -> Ctx
def bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def ctx_from(request: Request, raw: str = "", workspace_hint: str = "") -> Ctx:
    db = get_db()
    raw = raw or bearer(request) or request.query_params.get("key", "")
    if raw.startswith(KEY_PREFIX):
        ws = key_workspace(db, raw)
        if not ws:
            raise HTTPException(401, "invalid API key")
        return Ctx(db, ws, "admin", None, "key")
    user = session_user(db, raw or request.cookies.get(COOKIE, ""))
    if not user:
        raise HTTPException(401, "sign in first")
    hint = workspace_hint or request.headers.get("x-workspace", "") or request.query_params.get("ws", "")
    mine = workspaces_of(db, user["id"])
    if not mine:
        raise HTTPException(409, "you don't belong to a workspace yet")
    pick = next((w for w in mine if w["id"] == hint), None) if hint else mine[0]
    if not pick:
        raise HTTPException(403, "you're not a member of that workspace")
    ws = db.one("SELECT * FROM workspaces WHERE id = ?", [pick["id"]])
    return Ctx(db, ws, pick["role"], user, "session")


def ctx(request: Request) -> Ctx:
    """FastAPI dependency."""
    return ctx_from(request)


def current_user(request: Request) -> dict:
    db = get_db()
    raw = bearer(request)
    user = session_user(db, raw if raw and not raw.startswith(KEY_PREFIX) else request.cookies.get(COOKIE, ""))
    if not user:
        raise HTTPException(401, "sign in first")
    return user
